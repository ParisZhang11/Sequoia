import torch
from torch.nn.functional import softmax
from .Tree import Tree
import time
from Engine.Engine import GraphInferenceEngine, GraphInferenceEngineTG
from utils import get_sampling_logits, ChildrenAccept, get_residual, sampling_without_replacement
from collections import defaultdict

class SpecTree(Tree):
    def __init__(self, 
                 draft_model_engine :GraphInferenceEngine,
                 target_model_engine :GraphInferenceEngineTG,
                 prefix :torch.LongTensor,
                 temperature :float = 0.6,
                 top_p: float = 0.9,
                 draft_kv_len = 0,
                 target_kv_len = 0,
                 max_length = 256,
                 device :str = 'cpu',
                 max_target_seq = 256,
                 vocab_size = 32000,
                 grow_map = None,
                 attn_mask = None, 
                 sequence = None, 
                 new_tokens_buffer = None, 
                 parents_buffer = None, 
                 position_ids = None,
                 residual_graph = None,
                 sampling_callables = None,
                 sample_gather_indices = None,
                 tree_size = 32,
                 tree_depth = 6) -> None:
        super().__init__(device=device, max_length=max_length)
        assert self.max_length == draft_model_engine.engine.max_length
        self.max_target_seq = max_target_seq
        self.draft_model_engine = draft_model_engine
        self.target_model_engine = target_model_engine
        self.temperature = temperature
        self.top_p = top_p
        self.residual_graph = residual_graph
        self.grow_map = grow_map
        self.sampling_callables = sampling_callables
        self.sample_gather_indices = sample_gather_indices
        self.draft_step = len(self.grow_map["roots"])
        self.grow_map_roots_gpu = []
        for x in self.grow_map["roots"]:
             self.grow_map_roots_gpu.append(torch.Tensor(x).to(self.device).long())
        # [[1, 2, 3, 4, 5, 6, 7, 8], [9, 10, 11, 12, 13], [14, 15], [16], [17], [18], [], [], [], [19, 20, 21], [22], [23], [], [], [24], [], [25], [], [], [26, 27], [28], [], [29], [], [30], [], [31], [], [], [], [], []]
        self.Successors = self.grow_map["Successors"]
        tree_mask :torch.Tensor = self.grow_map["mask"].to(self.device)
        tree_mask = (tree_mask == 0).type(self.dtype)
        
        tree_mask.masked_fill_(tree_mask > 0, torch.finfo(self.dtype).min)
        self.initialize(attn_mask, sequence, new_tokens_buffer, parents_buffer, position_ids, None) # self.full_attn_mask = attn_mask.repeat(2, 2) -> (max_length*2, max_length*2)
        self.set_prefix(prefix=prefix)  # set full_attn_mask[:self.max_length, :self.max_length] to be normal causal mask
        # self.tree_size = self.grow_map["size"]
        self.tree_mask = tree_mask

        self.full_attn_mask[self.max_length - self.tree_size + 1: self.max_length, self.max_length - self.tree_size + 1: self.max_length] = tree_mask[1:, 1:]   # set last [self.tree_size, self.tree_size] of the causal mask at the first half of [:self.max_length, :self.max_length] to be tree_mask[1:, 1:]

        total_nodes = len(prefix) + self.tree_size - 1
        self.attn_mask = self.full_attn_mask[self.max_length - total_nodes: 2 * self.max_length - total_nodes, self.max_length - total_nodes: 2 * self.max_length - total_nodes]    # update attn_mask to be full_attn_mask[max_length - total_nodes : 2*max_length - total_nodes] (normal causal mask for prefix + tree mask for tree nodes, extend until length=max_length, but mask will be min for all the nodes after tree size)
        self.ground_truth_len = len(prefix)
        self.r = torch.rand(len(position_ids), dtype=self.dtype).to(self.device)
        
        # TODO: update position_ids
        self.position_ids[len(prefix) : len(prefix) + self.tree_size - 1] = (self.grow_map["depth"][1:].to(self.device) + len(prefix) - 1)
        self.storage_ids = torch.arange(self.max_length).to(self.device)
        # tensor([0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5])
        # self.depth = self.grow_map["depth"][1:].to(self.device)
        
        self.draft_logits = torch.zeros((self.max_length, vocab_size), dtype=self.dtype).to(self.device)
        if draft_kv_len == 0:
            draft_model_outputs = self.draft_model_engine.inference(input_ids=self.tokens[:self.num_nodes].unsqueeze(0), 
                                storage_ids=self.storage_ids[:self.num_nodes], 
                                position_ids=self.position_ids[:self.num_nodes].unsqueeze(0),
                                attn_mask=self.attn_mask[:self.num_nodes][None, None, :, :])
            self.draft_logits[0] = draft_model_outputs[...,-1,:][0]
        
        else:
            draft_model_outputs = self.draft_model_engine.inference(input_ids = self.tokens[draft_kv_len: self.num_nodes].unsqueeze(0), 
                                                    storage_ids=self.storage_ids[draft_kv_len: self.num_nodes],
                                                    position_ids=self.position_ids[draft_kv_len: self.num_nodes].unsqueeze(0),
                                                    attn_mask=self.attn_mask[draft_kv_len: self.num_nodes][None, None, :, :])
            self.draft_logits[0] = draft_model_outputs[...,-1,:][0]
        self.draft_kv_len = self.num_nodes
        
        self.target_kv_len = target_kv_len
        
        self.rand = torch.empty((self.tree_size, self.draft_logits.shape[1]), dtype=self.dtype).uniform_().to(self.device)
        self.seq_to_use = list(range(self.max_length))

    
    @torch.inference_mode()
    def collective_grow_static(self, idx_list :list[int], n_branch_list :list[int], benchmark=False, grow_step = None):
        
        if benchmark:
            x1 = 0.0
            x2 = 0.0
        
        
        
        
        total_branch = sum(n_branch_list)

        if benchmark:
                torch.cuda.synchronize()
                t1 = time.time()

        # In SpecInferTree, 
        # max_branch = max(n_branch_list)
        # sampling_logits = self.draft_logits[idx_list]
        # sampling_q = softmax(sampling_logits / self.temperature, dim=-1)
        # new_tokens_set = sampling_q.multinomial(num_samples=max_branch, replacement=True).flatten()

        # TODO: add code to decide the number of branches for each generated node according to confidence_scores
        # new_tokens_set :torch.LongTensor = self.sampling_callables[grow_step](self.draft_logits[idx_list], self.rand[idx_list])
        new_tokens_set, confidence_scores = sampling_without_replacement(self.draft_logits[idx_list], self.rand[idx_list], total_branch, self.temperature)
        self.tokens[self.num_nodes: self.num_nodes + total_branch] = new_tokens_set[self.sample_gather_indices[grow_step]]
        if benchmark:
                    torch.cuda.synchronize()
                    t2 = time.time()
                    x1 += (t2 - t1)
            
        self.num_nodes = self.num_nodes + total_branch
        

        
        start_pos = self.num_nodes - total_branch
        end_pos = self.num_nodes
        attn_mask = self.attn_mask[self.num_nodes - total_branch: self.num_nodes]   # attn_mask from the previous tokens to current all output (+ total_branch)
        attn_mask = attn_mask[None, None, :, :]
        
        draft_model_outputs = self.draft_model_engine.graph_inference(
            input_ids = self.tokens[self.draft_kv_len: self.num_nodes].unsqueeze(0),
            position_ids = self.position_ids[start_pos : end_pos].unsqueeze(0),
            attn_mask = attn_mask,
            storage_ids=self.storage_ids[self.draft_kv_len: self.num_nodes]
            
        )
        self.draft_kv_len = self.num_nodes
        self.draft_logits[start_pos - self.ground_truth_len + 1:end_pos - self.ground_truth_len + 1] = draft_model_outputs[0][-total_branch:]
        if benchmark:
                    torch.cuda.synchronize()
                    t3 = time.time()
                    x2 += (t3 - t2)
        if benchmark:
            return n_branch_list, x1, x2
        return n_branch_list
    
    @torch.inference_mode()
    def accept_step(self, parent_id :int):
        logits_id = parent_id - (self.ground_truth_len - 1)
        p = self.target_logits[logits_id]
        draft_logits = self.draft_logits[logits_id]
        
        children = self.Successors[logits_id]
        if len(children) == 0:
            return (-1, p)
        
        for idx, pos in enumerate(children):

            token = self.tokens[pos + (self.ground_truth_len - 1)]
            q = softmax(draft_logits / self.temperature, dim=-1)
            r = self.r[pos + (self.ground_truth_len - 1)]
            
            if p[token] > r * q[token]:
                # self.accept_idx_map[idx] += 1
                return (pos + (self.ground_truth_len - 1), None)
                # return (-1, p)
            else:
                p = self.residual_graph(p, q)
                draft_logits[token] = torch.finfo(self.dtype).min
        # self.accept_idx_map[-1] += 1
        return (-1, p)

    @torch.inference_mode()
    def verify(self, benchmark = False):
        new_node_num = (self.num_nodes - self.ground_truth_len + 1)
        if self.target_kv_len == 0:
            start_pos = 0
            end_pos = self.num_nodes
            attn_mask = self.attn_mask[start_pos: end_pos, :end_pos]
            attn_mask = attn_mask[None, None, :, :].type(self.target_model_engine.dtype)
            if benchmark:
                torch.cuda.synchronize()
                t1 = time.time()
            target_model_outputs = self.target_model_engine.inference(input_ids = self.tokens[start_pos : end_pos].unsqueeze(0), 
                                    position_ids = self.position_ids[start_pos : end_pos].unsqueeze(0), attn_mask = attn_mask, 
                                    storage_ids=self.storage_ids[start_pos : end_pos])
            if benchmark:
                torch.cuda.synchronize()
                t2 = time.time()
            self.target_logits :torch.FloatTensor= target_model_outputs[0][self.ground_truth_len - 1:]
            
        else:
            start_pos = self.target_kv_len
            end_pos = self.num_nodes
            attn_mask = self.attn_mask[start_pos: end_pos, :end_pos]
            attn_mask = attn_mask[None, None, :, :].type(self.target_model_engine.dtype)
            if benchmark:
                torch.cuda.synchronize()
                t1 = time.time()
            target_model_outputs = self.target_model_engine.inference(input_ids = self.tokens[start_pos : end_pos].unsqueeze(0), 
                                        position_ids =self.position_ids[start_pos : end_pos].unsqueeze(0), attn_mask = attn_mask,
                                        storage_ids=self.storage_ids[start_pos : end_pos])
            if benchmark:
                torch.cuda.synchronize()
                t2 = time.time()
            self.target_logits :torch.FloatTensor = target_model_outputs[0][-(new_node_num):]
        
        assert len(self.target_logits) == (self.num_nodes - self.ground_truth_len + 1)

        self.target_logits = get_sampling_logits(logits=self.target_logits, top_p=self.top_p, T=self.temperature, replicate=False)
        
        self.target_logits = softmax(self.target_logits / self.temperature, dim=-1)
        
        accept_list = self.seq_to_use[:self.ground_truth_len]
        
        terminal = False
        # Recursively check if any children will be accepted
        # pos is the accepted position, res is the residual probability if no position is accepted (used to randomly sample)
        while True:
            parent_id = accept_list[-1]
            pos, res = self.accept_step(parent_id=parent_id)
            if pos != -1:
                accept_list.append(pos)
                if self.tokens[pos] == 0 or self.tokens[pos] == 2:
                     terminal = True
                     break
            else:
                residual = res
                break
        if benchmark:
            torch.cuda.synchronize()
            t3 = time.time()
        accept_length = len(accept_list)
        if not terminal:
            if torch.isnan(residual).any():
                 terminal = True
            else:
                self.tokens[accept_length] = residual.multinomial(num_samples=1, replacement=True)

        # accept_list is a list of position indices
        self.tokens[:accept_length] = self.tokens[accept_list]

        self.draft_model_engine.engine.kv_cache.gather_kv_incremental(accept_list[self.ground_truth_len:], self.ground_truth_len)
        self.target_model_engine.engine.kv_cache.gather_kv_incremental(accept_list[self.ground_truth_len:], self.ground_truth_len)

        if not terminal:
            if benchmark:
                torch.cuda.synchronize()
                t4 = time.time()
                self.prepare_for_next_iter(accept_list, self.tokens[:accept_length+1])
                return self.tokens[:accept_length+1], accept_length, accept_length, t2 - t1, t3-t2, t4 - t3, terminal
            self.prepare_for_next_iter(accept_list, self.tokens[:accept_length+1])
            return self.tokens[:accept_length+1], accept_length, accept_length, terminal
        else:
             if benchmark:
                torch.cuda.synchronize()
                t4 = time.time()
                return self.tokens[:accept_length], accept_length, accept_length, t2 - t1, t3-t2, t4 - t3, terminal
             return self.tokens[:accept_length], accept_length, accept_length, terminal
    def verbose(self):
        super().verbose()
    def construct_grow_map(self, benchmark = False):
        if benchmark:
            sample_time = 0
            compute_time = 0
        # iterate each layer of the tree, sample tokens
        for i in range(self.draft_step - 1):
                if benchmark:
                        _, t1, t2 = self.collective_grow_static(self.grow_map_roots_gpu[i], self.grow_map['branches'][i], benchmark=benchmark, grow_step=i)
                        sample_time += t1
                        compute_time += t2   
                else:
                        # grow_map_roots_gpu = [[0], [1, 2, 3, 4, 5, 6, 7, 8], [9, 10, 11, 12, 13, 14, 15, 16, 17, 18], [19, 20, 21, 22, 23, 24, 25], [26, 27, 28, 29, 30], [31]]
                        # grow_map['branches'] = [[8], [5, 2, 1, 1, 1, 0, 0, 0], [3, 1, 1, 0, 0, 1, 0, 1, 0, 0], [2, 1, 0, 1, 0, 1, 0], [1, 0, 0, 0, 0], [0]]
                        # TODO: decide the number of branches to grow within collect_grow_static; update grow_map_roots and branches here
                        self.collective_grow_static(self.grow_map_roots_gpu[i], self.grow_map['branches'][i], grow_step=i)
        if benchmark:
            return sample_time, compute_time
        else:
            return None
    
    def prepare_for_next_iter(self, accept_list: list[int], valid_tokens :torch.LongTensor):
        if len(accept_list) + 1 > self.max_target_seq:
              return 
        self.position_ids[:len(accept_list)] =  self.position_ids[accept_list]
        self.position_ids[len(accept_list)] = len(accept_list) 
        self.position_ids[len(valid_tokens) : len(valid_tokens) + self.tree_size - 1] = (self.depth + len(valid_tokens) - 1)
        self.ground_truth_len = len(valid_tokens)
        self.num_nodes = len(valid_tokens)

        total_nodes = len(valid_tokens) + self.tree_size - 1
        self.attn_mask = self.full_attn_mask[self.max_length - total_nodes: 2 * self.max_length - total_nodes, self.max_length - total_nodes: 2 * self.max_length - total_nodes]

        
        draft_model_outputs = self.draft_model_engine.graph_inference(input_ids = self.tokens[len(accept_list): self.num_nodes].unsqueeze(0), 
                                                    storage_ids=self.storage_ids[len(accept_list): self.num_nodes],
                                                    position_ids=self.position_ids[len(accept_list): self.num_nodes].unsqueeze(0),
                                                    attn_mask=self.attn_mask[len(accept_list): self.num_nodes][None, None, :, :])
        
        self.draft_logits[0] = draft_model_outputs[...,-1,:][0]
        self.draft_kv_len = self.num_nodes
        self.target_kv_len = len(accept_list)
        


        


class SpecTreeTest(Tree):
    def __init__(self, 
                 draft_model_engine :GraphInferenceEngine,
                 target_model_engine :GraphInferenceEngineTG,
                 prefix :torch.LongTensor,
                 temperature :float = 0.6,
                 top_p: float = 0.9,
                 draft_kv_len = 0,
                 target_kv_len = 0,
                 max_length = 256,
                 max_width = 32,
                 device :str = 'cpu',
                 attn_mask = None, 
                 sequence = None, 
                 new_tokens_buffer = None, 
                 parents_buffer = None, 
                 position_ids = None) -> None:
        
        super().__init__(device=device, max_length=max_length)
        assert self.max_length == draft_model_engine.engine.max_length
        self.max_width = max_width
        self.draft_model_engine = draft_model_engine
        self.target_model_engine = target_model_engine
        self.temperature = temperature
        self.top_p = top_p
        
        self.initialize(attn_mask, sequence, new_tokens_buffer, parents_buffer, position_ids, None)
        self.set_prefix(prefix=prefix)
        self.Successors = [list(range(1, self.max_width + 1))]
        self.Successors.extend([[] for _ in range(self.max_width)])

        self.attn_mask = self.full_attn_mask[:self.max_length, :self.max_length]
        for idx in range(self.max_width):
             self.attn_mask[idx + self.num_nodes] = self.attn_mask[self.num_nodes - 1]
             self.attn_mask[idx + self.num_nodes][idx + self.num_nodes] = 0.0
        
        self.position_ids[self.num_nodes : self.num_nodes + self.max_width] = self.position_ids[self.num_nodes - 1] + 1
        self.ground_truth_len = len(prefix)
        self.r = torch.rand(len(position_ids)).to(self.device)
        self.storage_ids = torch.arange(self.max_length).to(self.device)
        
        
        if draft_kv_len == 0:
            draft_model_outputs = self.draft_model_engine.inference(input_ids=self.tokens[:self.num_nodes].unsqueeze(0), 
                                storage_ids=self.storage_ids[:self.num_nodes], 
                                position_ids=self.position_ids[:self.num_nodes].unsqueeze(0),
                                attn_mask=self.attn_mask[:self.num_nodes][None, None, :, :])
            self.draft_logits :torch.FloatTensor= draft_model_outputs[...,-1,:]
        
        else:
            draft_model_outputs = self.draft_model_engine.inference(input_ids = self.tokens[draft_kv_len: self.num_nodes].unsqueeze(0), 
                                                    storage_ids=self.storage_ids[draft_kv_len: self.num_nodes],
                                                    position_ids=self.position_ids[draft_kv_len: self.num_nodes].unsqueeze(0),
                                                    attn_mask=self.attn_mask[draft_kv_len: self.num_nodes][None, None, :, :])
            self.draft_logits :torch.FloatTensor = draft_model_outputs[...,-1,:]
        self.draft_kv_len = self.num_nodes
        
        self.target_kv_len = target_kv_len
        self.rand = torch.empty((self.max_width + 1, self.draft_logits.shape[1])).uniform_().to(self.device)
        self.collective_grow_static([0], [self.max_width])
    
    @torch.inference_mode()
    def collective_grow_static(self, idx_list :torch.LongTensor, n_branch_list :list[int], benchmark=False):
        
        
        assert len(set(idx_list)) == len(idx_list)
        assert len(self.draft_logits) == (self.num_nodes - self.ground_truth_len + 1)
        
        total_branch = sum(n_branch_list)
        max_branch = max(n_branch_list)
        sampling_logits = self.draft_logits[idx_list]
        
        sampling_q = softmax(sampling_logits / self.temperature, dim=-1)
        
            
            
        new_tokens_set  = (self.rand[idx_list].log()/sampling_q).topk(k=max_branch).indices
        
            
        
        finished_tokens = 0
            
        for i, idx in enumerate(idx_list):
                n_branch = n_branch_list[i]
                self.tokens[self.num_nodes + finished_tokens: self.num_nodes + finished_tokens + n_branch]  = new_tokens_set[i][:n_branch]
                finished_tokens += n_branch
            
        
        self.num_nodes = self.num_nodes + total_branch
        

        
        start_pos = self.num_nodes - total_branch
        end_pos = self.num_nodes
        attn_mask = self.attn_mask[self.num_nodes - total_branch: self.num_nodes]
        attn_mask = attn_mask[None, None, :, :]
        
        draft_model_outputs = self.draft_model_engine.graph_inference(
            input_ids = self.tokens[self.draft_kv_len: self.num_nodes].unsqueeze(0),
            position_ids = self.position_ids[start_pos : end_pos].unsqueeze(0),
            attn_mask = attn_mask,
            storage_ids=self.storage_ids[self.draft_kv_len: self.num_nodes]
            
        )
        self.draft_kv_len = self.num_nodes
        self.draft_logits = torch.cat([self.draft_logits, draft_model_outputs[0][-total_branch:]], dim=0)
        assert len(self.draft_logits) == (self.num_nodes - self.ground_truth_len + 1)
        
        return n_branch_list
    @torch.inference_mode()
    def accept_step(self, parent_id :int) ->ChildrenAccept:
        logits_id = parent_id - (self.ground_truth_len - 1)
        p = self.target_logits[logits_id]
        
        draft_logits = self.draft_logits[logits_id]
        children = self.Successors[logits_id]
        if len(children) == 0:
            return ChildrenAccept(accept_mark=2, residual=p)
        
        for idx, pos in enumerate(children):

            token = self.tokens[pos + (self.ground_truth_len - 1)]
            q = softmax(draft_logits / self.temperature, dim=-1)
            r = self.r[pos + (self.ground_truth_len - 1)]
            if p[token] >= r * q[token]:
                # print(parent_id, idx)
                return ChildrenAccept(accept_mark=0, token=token, position=pos + (self.ground_truth_len - 1), successor_order=idx)
            else:
                p = get_residual(p, q)
                draft_logits[token] = -torch.inf
        
        return ChildrenAccept(accept_mark=1, residual=p)


        
    @torch.inference_mode()
    def verify(self, benchmark = False):
        new_node_num = (self.num_nodes - self.ground_truth_len + 1)
        if self.target_kv_len == 0:
            start_pos = 0
            end_pos = self.num_nodes
            attn_mask = self.attn_mask[start_pos: end_pos, :end_pos]
            attn_mask = attn_mask[None, None, :, :].type(self.target_model_engine.dtype)
            target_model_outputs = self.target_model_engine.inference(input_ids = self.tokens[start_pos : end_pos].unsqueeze(0), 
                                    position_ids = self.position_ids[start_pos : end_pos].unsqueeze(0), attn_mask = attn_mask, 
                                    storage_ids=self.storage_ids[start_pos : end_pos])
            self.target_logits :torch.FloatTensor= target_model_outputs[0][self.ground_truth_len - 1:]
            
        else:
            start_pos = self.target_kv_len
            end_pos = self.num_nodes
            attn_mask = self.attn_mask[start_pos: end_pos, :end_pos]
            attn_mask = attn_mask[None, None, :, :].type(self.target_model_engine.dtype)
            target_model_outputs = self.target_model_engine.inference(input_ids = self.tokens[start_pos : end_pos].unsqueeze(0), 
                                        position_ids =self.position_ids[start_pos : end_pos].unsqueeze(0), attn_mask = attn_mask,
                                        storage_ids=self.storage_ids[start_pos : end_pos])
            
            self.target_logits :torch.FloatTensor = target_model_outputs[0][-(new_node_num):]
        
        assert len(self.draft_logits) == (self.num_nodes - self.ground_truth_len + 1)
        assert len(self.target_logits) == (self.num_nodes - self.ground_truth_len + 1)
        self.target_logits = get_sampling_logits(logits=self.target_logits, top_p=self.top_p, T=self.temperature, replicate=False)
        self.target_logits = softmax(self.target_logits / self.temperature, dim=-1)
        accept_list = list(range(self.ground_truth_len))
        b = -1
        terminal = False
        while True:
            parent_id = accept_list[-1]
            children_accept = self.accept_step(parent_id=parent_id)
            if children_accept.accept_mark == 0:
                accept_list.append(children_accept.position)
                b = children_accept.successor_order
                if self.tokens[children_accept.position] == 2 or self.tokens[children_accept.position] == 0:
                     terminal = True
                     break
            else:
                residual = children_accept.residual
                break
        if not terminal:
            if torch.isnan(residual).any():
                 terminal = True
            else:
                last_token = residual.multinomial(num_samples=1, replacement=True)

        
        accept_tokens = self.tokens[accept_list]
        if not terminal:
            valid_tokens = torch.cat([accept_tokens, last_token], dim=-1)
            
            self.draft_model_engine.gather_kv(accept_list)
            self.target_model_engine.gather_kv(accept_list)

            return valid_tokens, len(accept_list), len(accept_list), b, terminal
        else:
            return accept_tokens, len(accept_list), len(accept_list), b, terminal
    
    def verbose(self):
        super().verbose()

    
    

                
