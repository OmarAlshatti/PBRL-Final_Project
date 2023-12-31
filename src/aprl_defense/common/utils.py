from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Dict

import gym
import numpy as np
import pyspiel
import ray
from gym.spaces import Box
from pettingzoo.classic import rps_v2
from pettingzoo.atari import pong_v2
from ray.rllib import RolloutWorker, Policy, BaseEnv
from ray.rllib.agents import DefaultCallbacks
from ray.rllib.agents.a3c import A3CTrainer, A2CTrainer
from ray.rllib.agents.ddpg import DDPGTrainer
from ray.rllib.agents.impala import ImpalaTrainer
from ray.rllib.agents.ppo import PPOTrainer, APPOTrainer
from ray.rllib.agents.sac import SACTrainer

# from ray.rllib.contrib.maddpg import MADDPGTrainer  # Bugs in the current version of RLLIB MADDPGTrainer
from ray.rllib.env import PettingZooEnv
from ray.rllib.env.wrappers.open_spiel import OpenSpielEnv
from ray.rllib.evaluation import MultiAgentEpisode
from ray.tune import register_env
from ray.tune.logger import NoopLogger

# from aprl_defense.agents.maddpg import MADDPGTrainer
from aprl_defense.common.base_logger import logger
from aprl_defense.common.io import get_saved_config
from aprl_defense.configs.reward import default
from aprl_defense.envs.open_spiel_zs_env import OpenSpielZSEnv
from ext.aprl.training.shaping_wrappers import apply_reward_wrapper
from ext.envs.multiagent_particle_env import (
    RLlibMultiAgentParticleEnv,
    RLlibMultiAgentParticleEnv as MultiAgentParticleEnv,
)

trainer_map = {
    #    'maddpg': MADDPGTrainer,
    "ppo": PPOTrainer,
    "sac": SACTrainer,
    "a3c": A3CTrainer,
    "a2c": A2CTrainer,
    "ddpg": DDPGTrainer,  # Doesn't work in simple-push (discrete actions)
    "appo": APPOTrainer,
    "impala": ImpalaTrainer,
}


def trainer_cls_from_str(trainer_name: str):
    if trainer_name in trainer_map:
        return trainer_map[trainer_name]
    else:
        raise ValueError(f"Invalid RL algorithm: {trainer_name}")


def generate_multiagent_shared_policy():
    multiagent = {
        "policies": {"shared_policy"},
        "policy_mapping_fn": (lambda agent_id, episode, **kwargs: "shared_policy"),
    }
    return multiagent


def generate_multiagent_2_policies(env, policies_to_train: Optional[List[str]]) -> dict:
    num_policies = 2

    policy_ids = []
    for i in range(0, num_policies):
        policy_ids.append(f"policy_{i}")

    if isinstance(env, PettingZooEnv):
        # For PZ the policies dict is created automatically, so we only need to pass the policy names
        agent_ids = list(env.agents)
        policies = set(policy_ids)
    else:  # if isinstance(env, RLlibMultiAgentParticleEnv) or isinstance(env, OpenSpielEnv):
        # Currently we need to create the policies dict for MPE and OS manually

        action_spaces, observation_spaces, agent_ids = spaces_from_env(env)
        policies = {}
        for i in range(0, num_policies):
            policies[f"policy_{i}"] = (
                None,
                observation_spaces[i],
                action_spaces[i],
                {
                    "agent_id": i,
                },
            )

    assert len(agent_ids) == 2, "Currently only 2-agent environments are supported"

    agent_to_policy = {
        agent_id: policy_id for agent_id, policy_id in zip(agent_ids, policy_ids)
    }

    policy_mapping_func = lambda agent_id, episode, **kwargs: agent_to_policy[agent_id]

    multiagent = {
        "policies": policies,
        "policy_mapping_fn": policy_mapping_func,
        "policies_to_train": policies_to_train,
    }

    return multiagent


def spaces_from_env(env):
    """Returns two dictionaries where the element at key i corresponds the action and observation spaces respectively for agent with id i."""
    if isinstance(
        env, OpenSpielEnv
    ):  # This env has a constant observation+action space for all agents
        agent_ids = list(range(env.num_agents))
        # New theory: The following seems to only be necessary in ray 1.6 onwards
        # Yep, for some reason there observations have np.float64, while the original observation space is np.float32. I'm not sure where exactly the cause of
        # this problem is, so here is my hacky workaround. Unfortunately this means we reset the seed, because the original seed is not saved. Alternatively,
        # we could set the ._no_random attribute, but this doesn't seem necessary
        new_observation_space = Box(
            low=env.observation_space.low,
            high=env.observation_space.high,
            shape=env.observation_space.shape,
            dtype=np.float64,
        )
        observation_spaces = {agent_id: new_observation_space for agent_id in agent_ids}
        action_spaces = {agent_id: env.action_space for agent_id in agent_ids}
    elif isinstance(env, RLlibMultiAgentParticleEnv):
        agent_ids = env._agent_ids
        observation_spaces = env.observation_space_dict
        action_spaces = env.action_space_dict
    elif isinstance(env, PettingZooEnv):
        agent_ids = list(env.agents)
        observation_spaces = env.observation_spaces
        action_spaces = env.action_spaces
    elif isinstance(env, gym.Env):
        try:
            from aprl_defense.common.wrappers import MujocoToRllibWrapper

            if isinstance(env, MujocoToRllibWrapper):
                agent_ids = list(env.unwrapped.agents.keys())
                observation_spaces = env.observation_space
                action_spaces = env.action_space
            else:
                agent_ids = None
                observation_spaces = None
                action_spaces = None
        except ImportError:
            logger.info("Mujoco not installed")

            # Keep this at the bottom, as many of the other envs are subclasses of
            # gym.Env
            # Not needed for single-agent env
            agent_ids = None
            observation_spaces = None
            action_spaces = None
    else:
        raise NotImplementedError(
            f"Environment {env} with type {type(env)} not supported!"
        )
    return action_spaces, observation_spaces, agent_ids


def get_base_train_config(alg):
    # Normalize actions is not available for multi-agent envs, at least according to
    # this: https://github.com/ray-project/ray/issues/8518k
    # For me this problem only occured with SAC, DDPG
    config = {"normalize_actions": False}
    return config


def noop_logger_creator(config):
    """Creator function for NoopLogger. Trainable receive a creator which has the single argument 'config'. Because this creates a NoopLogger the argument
    is never used."""
    return NoopLogger(config, "")


def create_trainer(trainer_cls, config):
    return trainer_cls(
        env="current-env", config=config  # , logger_creator=noop_logger_creator
    )


def policies_equal(policy_1, policy_2) -> bool:
    """Compare structural equality of two policies."""
    if (isinstance(policy_1, OrderedDict) and isinstance(policy_2, OrderedDict)) or (
        isinstance(policy_1, dict) and isinstance(policy_2, dict)
    ):
        if not policy_1.keys() == policy_2.keys():
            return False
        for key in policy_1.keys():
            element_1: np.ndarray = policy_1[key]
            element_2: np.ndarray = policy_2[key]
            if not np.all(element_1 == element_2):
                return False
        return True
    else:
        raise NotImplementedError(
            "Currently we only support RLLib policies which are ordered dicts (which applies to at least PPO)."
            f"Instead received {type(policy_1)} and {type(policy_2)}"
        )


class CustomMujocoMetricsCallbacks(DefaultCallbacks):
    def on_episode_end(
        self,
        worker: RolloutWorker,
        base_env: BaseEnv,
        policies: Dict[str, Policy],
        episode: MultiAgentEpisode,
        **kwargs,
    ):
        log_dict = {}
        for agent_id in range(worker.env.unwrapped.num_agents):
            last_info = episode.last_info_for(agent_id)
            for rew_type, rew in last_info["logged_agent_rewards"].items():
                log_dict[f"agent_{agent_id}_{rew_type}_reward"] = rew

            log_dict[f"agent_{agent_id}_dense_weight"] = last_info["dense_weight"]

        episode.custom_metrics.update(log_dict)


def init_env(
    env_name: str, scenario_name: str, scheduler=None, max_steps=None, mujoco_state=None
):
    if env_name == "mpe":
        # Initialize environment
        def create_mpe_env(mpe_args):
            env = MultiAgentParticleEnv(max_steps=max_steps, **mpe_args)
            # env = Float64To32Wrapper(env)  # Apply float wrapper

            # The following only works with gym or pettingzoo envs
            # def action_wrapper(action, space):  # Convert discrete to one-hot actions, for mpe env
            #     one_hot = np.zeros((1, 5))
            #     discrete_action = action[0]
            #     one_hot[discrete_action] = 1.0
            #     return one_hot
            #
            # if alg != 'maddpg':  # MADDPG is the only alg that already returns one-hot activations
            #     env = action_lambda_v1(env,
            #                            action_wrapper,
            #                            lambda space: space)

            return env

        register_env("current-env", create_mpe_env)
        env = create_mpe_env({"scenario_name": scenario_name})
    elif env_name == "gym":

        def env_creator(args):
            env = gym.make(scenario_name)
            return env

        env = env_creator({})
        register_env("current-env", env_creator)
    elif env_name == "open_spiel":
        # '_zs_' suffix marks the envs that should be made zero-sum
        if scenario_name.endswith("_zs"):
            scenario_name = scenario_name[:-3]  # Drop the suffix
            env_wrapper = OpenSpielZSEnv
            logger.info(f"USING ZERO SUM VERSION OF ENV {scenario_name}")
        else:
            env_wrapper = OpenSpielEnv

        def create_os_env(_):
            return env_wrapper(pyspiel.load_game(scenario_name))

        register_env("current-env", create_os_env)
        env = create_os_env({})
    elif env_name == "pettingzoo":
        if scenario_name == "rps":

            def env_creator(args):
                return PettingZooEnv(rps_v2.env(num_actions=3, max_cycles=15))

        elif scenario_name == "pong":

            def env_creator(args):
                return PettingZooEnv(pong_v2.env(num_players=2))

        else:
            raise NotImplementedError(
                f"Currently pettingzoo env {scenario_name} is not supported!"
            )

        env = env_creator({})
        register_env("current-env", env_creator)
    elif env_name == "multicomp":

        def env_creator(args):
            # noinspection PyUnresolvedReferences
            import gym_compete  # noqa: F401 Necessary so gym_compete envs are registered
            from aprl_defense.common.wrappers import (
                MujocoToRllibWrapper,
                MujocoEnvFromStateWrapper,
            )

            env = gym.make(f"multicomp/{scenario_name}")

            shaping_params = default
            if (
                scheduler is not None
            ):  # Apply reward shaping with scheduler only if scheduler is provided
                env = apply_reward_wrapper(env, shaping_params, scheduler)
            # Apply wrapper for RLlib compatibility
            env = MujocoToRllibWrapper(env)
            if mujoco_state is not None:
                env = MujocoEnvFromStateWrapper(env, mujoco_state)
            return env

        env = env_creator({})
        register_env("current-env", env_creator)
    else:
        raise ValueError(f"environment {env_name} not supported")
    return env


def load_saved_weights(
    victim_path: Path,
    scenario_name: str,
    victim_name: str,
    trainer_cls,
    config: Optional[dict] = None,
):
    victim_trainer = load_saved_checkpoint_for_eval(
        scenario_name, trainer_cls, victim_path, config
    )
    victim_weights = victim_trainer.get_weights(victim_name)
    return victim_weights


def load_saved_checkpoint_for_eval(
    scenario_name, trainer_cls, victim_path, config: Optional[dict] = None
):
    """We load the saved checkpoint with the saved config. Since we only load the
    checkpoint for some kind of eval or baseline, we are ultimately only interested in
    the weights. Some settings in the config might require certain paths to
    exist. However, we don't care about these when we are only interested in the weights.
    That is why in this method we override these config settings so that we don't need
    to make sure that these paths actually exist.
    We also provide the ability to evaluate in a different scenario than during
    training by changing the `scenario_name` param."""
    if config is None:
        config = get_saved_config(victim_path)
    # Changes to victim config for finetuning
    config["env"] = "current-env"
    config["num_workers"] = 0
    if "env_config" not in config:
        config["env_config"] = {}
    config["env_config"]["scenario_name"] = scenario_name
    if "multiagent" in config:
        config["multiagent"]["policy_map_capacity"] = 200
        config["multiagent"]["policy_map_cache"] = None
    if "evaluation_config" in config and "multiagent" in config["evaluation_config"]:
        config["evaluation_config"]["multiagent"]["policy_map_capacity"] = 200
        config["evaluation_config"]["multiagent"]["policy_map_cache"] = None
    # Restore the trained agent with this trainer
    new_trainer = create_trainer(trainer_cls, config)
    new_trainer.restore(checkpoint_path=str(victim_path))
    return new_trainer


def load_saved_checkpoint(trainer_cls, victim_path, config: Optional[dict] = None):
    """Load the checkpoint from the given path using the saved config."""
    if config is None:
        config = get_saved_config(victim_path)

    # Restore the trained agent with this trainer
    new_trainer = create_trainer(trainer_cls, config)
    new_trainer.restore(checkpoint_path=str(victim_path))
    return new_trainer
