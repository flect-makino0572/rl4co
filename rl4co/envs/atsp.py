from collections import defaultdict
from typing import Optional

import torch
from tensordict.tensordict import TensorDict
from torchrl.envs import EnvBase
from torchrl.data import BoundedTensorSpec, CompositeSpec, UnboundedContinuousTensorSpec, BinaryDiscreteTensorSpec, UnboundedDiscreteTensorSpec

from rl4co.utils.pylogger import get_pylogger
from rl4co.data.dataset import TensorDictDataset
from rl4co.envs.utils import batch_to_scalar
from rl4co.envs.base import RL4COEnv


log = get_pylogger( __name__ )


class ATSPEnv(RL4COEnv):
    batch_locked = False
    name = "atsp"

    def __init__(
        self,
        num_loc: int = 10,
        min_dist: float = 0,
        max_dist: float = 1,
        td_params: TensorDict = None,
        seed: int = None,
        device: str = "cpu",
    ):
        super().__init__(seed=seed, device=device)
        self.num_loc = num_loc
        self.min_dist = min_dist
        self.max_dist = max_dist
        self._make_spec(td_params)

    def _step(self, td: TensorDict) -> TensorDict:
        current_node = td["action"]
        first_node = current_node if batch_to_scalar(td["i"]) == 0 else td["first_node"]

        # Set available to 0 (i.e., we visited the node)
        available = td["action_mask"].scatter(
            -1, current_node[..., None].expand_as(td["action_mask"]), 0
        )

        # We are done there are no unvisited locations
        done = torch.count_nonzero(available.squeeze(), dim=-1) <= 0

        # The reward is calculated outside via get_reward for efficiency, so we set it to -inf here
        reward = torch.ones_like(done) * float("-inf")

        # The output must be written in a ``"next"`` entry
        return TensorDict(
            {
                "next": {
                    "observation": td["observation"],
                    "first_node": first_node,
                    "current_node": current_node,
                    "i": td["i"] + 1,
                    "action_mask": available,
                    "reward": reward,
                    "done": done,
                }
            },
            td.shape,
        )

    def _reset(
        self, td: Optional[TensorDict] = None, init_obs=None, batch_size=None
    ) -> TensorDict:
        # If no tensordict (or observations tensor) is passed, we generate a single set of hyperparameters
        # Otherwise, we assume that the input tensordict contains all the relevant parameters to get started.
        init_dm = td["observation"] if td is not None else init_obs # dm = distance matrix
        if batch_size is None:
            batch_size = self.batch_size if init_dm is None else init_dm.shape[:-2]
        device = init_dm.device if init_dm is not None else self.device
        self.device = device

        # We allow loading the initial observation from a dataset for faster loading
        if init_dm is None:
            # number generator is on CPU by default, set device after
            init_dm = self.generate_data(batch_size=batch_size).to(device)

        # Other variables
        current_node = torch.zeros((*batch_size, 1), dtype=torch.int64, device=device)
        available = torch.ones(
            (*batch_size, 1, self.num_loc), dtype=torch.bool, device=device
        )  # 1 means not visited, i.e. action is allowed
        i = torch.zeros((*batch_size, 1), dtype=torch.int64, device=device)

        return TensorDict(
            {
                "observation": init_dm,
                "first_node": current_node,
                "current_node": current_node,
                "i": i,
                "action_mask": available,
            },
            batch_size=batch_size,
        )

    def _make_spec(self, td_params: TensorDict = None):
        self.observation_spec = CompositeSpec(
            observation=BoundedTensorSpec(
                minimum=self.min_dist,
                maximum=self.max_dist,
                shape=(self.num_loc, self.num_loc),
                dtype=torch.float32,
            ),
            first_node=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            current_node=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            i=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            action_mask=UnboundedDiscreteTensorSpec(
                shape=(1, self.num_loc),
                dtype=torch.bool,
            ),
            shape=(),
        )
        self.input_spec = self.observation_spec.clone()
        self.action_spec = BoundedTensorSpec(
            shape=(1,),
            dtype=torch.int64,
            minimum=0,
            maximum=self.num_loc,
        )
        self.reward_spec = UnboundedContinuousTensorSpec(shape=(1,))
        self.done_spec = UnboundedDiscreteTensorSpec(shape=(1,), dtype=torch.bool)
    
    def get_reward(self, td, actions) -> TensorDict:
        distance_matrix = td["observation"]
        assert (
            torch.arange(actions.size(1), out=actions.data.new())
            .view(1, -1)
            .expand_as(actions)
            == actions.data.sort(1)[0]
        ).all(), "Invalid tour"
        
        # Get indexes of tour. Actions: [batch_size, num_loc]
        nodes_src = actions
        nodes_tgt = torch.roll(actions, 1, dims=1)
        batch_idx = torch.arange(distance_matrix.shape[0], device=distance_matrix.device).unsqueeze(1) 
        return distance_matrix[batch_idx, nodes_src, nodes_tgt].sum(-1)
    
    def dataset(self, batch_size):
        """Return a dataset of observations"""
        observation = self.generate_data(batch_size)
        return TensorDictDataset(observation)

    def generate_data(self, batch_size):
        # Generate distance matrices inspired by the reference MatNet (Kwon et al., 2021)
        # We satifsy the triangle inequality (TMAT class) in a batch 
        batch_size = [batch_size] if isinstance(batch_size, int) else batch_size
        dms = torch.rand((*batch_size, self.num_loc, self.num_loc), generator=self.rng) * (self.max_dist - self.min_dist) + self.min_dist 
        dms[..., torch.arange(self.num_loc), torch.arange(self.num_loc)] = 0
        while True:
            old_dms = dms.clone()
            dms, _ = (dms[..., :, None, :] + dms[..., None, :, :].transpose(-2,-1)).min(dim=-1)
            if (dms == old_dms).all():
                break
        return dms

    def render(self, td):
        try:
            import networkx as nx
        except ImportError:
            log.warn("Networkx is not installed. Please install it with `pip install networkx`")
            return
        
        td = td.detach().cpu()
        # if batch_size greater than 0 , we need to select the first batch element
        if td.batch_size != torch.Size([]):
            td = td[0]

        src_nodes = td['action']
        tgt_nodes = torch.roll(td['action'], 1, dims=0)

        # Plot with networkx
        G = nx.DiGraph(td['observation'].numpy())
        pos = nx.spring_layout(G)
        nx.draw(G, pos, with_labels=True, node_color='skyblue', node_size=800, edge_color='white')

        # draw edges src_nodes -> tgt_nodes
        edgelist = [ (src_nodes[i].item(), tgt_nodes[i].item()) for i in range(len(src_nodes)) ]
        nx.draw_networkx_edges(G, pos, edgelist=edgelist, width=2, alpha=1, edge_color='black')
