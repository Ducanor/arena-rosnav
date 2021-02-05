#3 (see il_state_collector.py) aim to create a customer policy to process supervised learning

#done  inherit A2C/PPO to write a A2C class with pretraining functions
#done  if necessary(it is) inherit nn.Module from pytorch and write a MLP (see requirements in def _calc_loss)

from stable_baselines3 import PPO

import warnings
from typing import Any, Dict, Optional, Type, Union

import numpy as np
import torch as th
import torch.optim as optim
from gym import spaces
import torch.nn as nn
from torch.autograd import Variable

from stable_baselines3.common import logger
from stable_baselines3.common.on_policy_algorithm import OnPolicyAlgorithm
from stable_baselines3.common.policies import ActorCriticPolicy
from stable_baselines3.common.type_aliases import GymEnv, MaybeCallback, Schedule
from stable_baselines3.common.utils import explained_variance, get_schedule_fn
from il_customer_policy import ILPolicy

import h5py
import glob
import time

import rospkg

class pretrainPPO(PPO):
    def __init__(
        self,
        policy: Union[str, Type[ActorCriticPolicy]],
        env: Union[GymEnv, str],
        learning_rate: Union[float, Schedule] = 3e-4,
        n_steps: int = 2048,
        batch_size: Optional[int] = 64,
        n_epochs: int = 10,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        clip_range: Union[float, Schedule] = 0.2,
        clip_range_vf: Union[None, float, Schedule] = None,
        ent_coef: float = 0.0,
        vf_coef: float = 0.5,
        max_grad_norm: float = 0.5,
        use_sde: bool = False,
        sde_sample_freq: int = -1,
        target_kl: Optional[float] = None,
        tensorboard_log: Optional[str] = None,
        create_eval_env: bool = False,
        policy_kwargs: Optional[Dict[str, Any]] = None,
        verbose: int = 0,
        seed: Optional[int] = None,
        device: Union[th.device, str] = "auto",
        _init_setup_model: bool = True,
        use_customer_policy: bool = True,
        dataset_length:int = 0
        ):

        super(pretrainPPO, self).__init__(policy, env,learning_rate, n_steps,batch_size,\
            n_epochs, gamma,gae_lambda, clip_range, clip_range_vf, ent_coef, vf_coef, max_grad_norm,\
                use_sde, sde_sample_freq, target_kl, tensorboard_log, create_eval_env, policy_kwargs, verbose, seed,device, _init_setup_model,
    )
        self._data_path = rospkg.RosPack().get_path('arena_local_planner_il') + '/data'
        # load h5 data to class
        self._load_data(dataset_length)

        # use customer defined policy
        if use_customer_policy:
            self.policy_class = ILPolicy
            self.policy = self.policy_class(self.observation_space,
                                            self.action_space,
                                            self.lr_schedule,
                                            use_sde=self.use_sde,
                                            batch_size=self.batch_size,
                                            **self.policy_kwargs  # pytype:disable=not-instantiable
                                            )
            self.policy.to(self.device)

    def _load_data(self, dataset_length):
        '''
        load data from h5 files and do numpy dataset initialization
        '''
        h5_path = glob.glob(self._data_path + '/*hdf5')
        if not h5_path:
            raise FileNotFoundError
        
        actions, states = {},{}
        for addr in h5_path:
            if addr.endswith('state.hdf5'):
                with h5py.File(addr, "r") as state_f:
                    for i in state_f.keys():
                        states[i] = np.array(state_f[i], dtype=np.float32)
                        
            elif addr.endswith('action.hdf5'):
                with h5py.File(addr, "r") as action_f:
                    for i in action_f.keys():
                        actions[i] = np.array(action_f[i][1], dtype=np.float32)

        self._actions_data = actions
        self._states_data = states
        # do data matching
        self._data_matching(dataset_length)

    def _data_matching(self, dataset_length):
        #before sampling first match actions and states according to time-slot
        self._dataset = []
        counter = 0
        for _, time_slot in enumerate(self._actions_data):
            if counter >= dataset_length:
                break
            action_key = time_slot
            # matching according to the mindest time gap principle
            state_key = min(self._states_data.keys(), key=lambda x: abs(float(action_key)-float(x)))
            self._dataset.append((self._actions_data[time_slot], self._states_data[state_key]))
            counter += 1
        print('dateset length: {} state-action pairs'.format(len(self._dataset)))

    def _sample_batch(self, batch_size):
        '''
        aim to sample batch from actions and states
        Use the pair principle to make the time stamp as close as possible
        '''
        index = np.random.randint(len(self._dataset), size=batch_size)
        batch = [self._dataset[i] for i in index]
        actions, states = [], []
        for data in batch:
            actions.append(data[0])
            states.append(data[1])
        # return actions and states respectively
        return np.array(actions, dtype=np.int32), np.array(states, dtype=np.float32)
                                  
    def pretrain(self, learning_rate, batch_size, policy_addr, cuda=0, epi=100, error_threshold=0.0001, loss_str="CE"):
        # self.policy.net_arch using FlattenExtractor (pi=[64, 64], vf=[64, 64])
        optimizer = optim.Adam(self.policy.parameters(), lr=learning_rate)

        if loss_str == "CE":
            objective = nn.CrossEntropyLoss()
        losses = 0
        last_1000ep_loss = 0
        iter_no = 0
        device = th.device("cuda" if cuda == 1 else "cpu")
        while True:
            iter_no += 1
            actions, states = self._sample_batch(batch_size)
            loss, pred_actions = self._calc_loss(actions, states, self.policy, objective, device=device)
            loss.requres_grad = True
            loss.backward()
            losses += loss.item()
            last_1000ep_loss += loss.item()

            if iter_no % 1000 == 0:
                last_1000ep_loss = last_1000ep_loss/1000
                print("episode: {} | evg_loss: {:.5f} | last_1000ep_loss: {:.5f}".format(iter_no, losses/iter_no, last_1000ep_loss))
                #print(pred_actions)
                if last_1000ep_loss < error_threshold or iter_no > epi:
                    print("training has been done!")
                    break
                last_1000ep_loss = 0
                if iter_no % 200000 == 0:
                    self.save_policy(policy_addr)
            
            optimizer.step()
            optimizer.zero_grad()
            
    def _calc_loss(self, actions, states, policy, objective, device="cpu"):
        actions = actions.astype(float)
        states = states.astype(float)
        
        actions_v = th.from_numpy(actions).float().to(device).long()
        states_v = th.from_numpy(states).float().to(device)
        states_v.requires_grad = True

        pred_actions, pred_values, log_prob, log_prob_list = policy(states_v) #need to return a action_prob distribution
        #print(log_prob_list.requires_grad) #should be true
        loss = objective(log_prob_list, actions_v)
        return loss, pred_actions

    def save_policy(self, addr:str):
        '''
        save policy to local addr
        '''
        import time
        file_name = '/{:.0f}.pth'.format(time.time())
        th.save(self.policy, addr + file_name)
