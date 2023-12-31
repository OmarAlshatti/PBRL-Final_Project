import time
from collections import defaultdict, deque
from copy import deepcopy
from typing import List, Optional

import gin
import wandb
from tqdm import tqdm

import aprl_defense.configs.eval
from aprl_defense.common.base_logger import logger
from aprl_defense.common.utils import (
    create_trainer,
    spaces_from_env,
    load_saved_weights,
    load_saved_checkpoint,
)
from aprl_defense.pbt.utils import (
    custom_eval_log,
    create_pbt_eval_func,
    create_policy_mapping_function,
)
from aprl_defense.training_managers.base_training_manager import (
    SingleJobTrainingManager,
)
from aprl_defense.trial.settings import TrialSettings, RLSettings


@gin.configurable(name_or_fn="pbt")
class PBTManager(SingleJobTrainingManager):
    """Train a single job of an agent hardened by PBT. A single agent will be trained against a population of opponents."""

    def __init__(
        self,
        trial_settings: TrialSettings,
        rl_settings: RLSettings,
        evaluation_freq: Optional[int] = None,
        main_id: int = 0,
        opponent_id: int =1,
        num_ops: int = 10,
        op_experience_factor: float = 1.0,
        baseline_artifacts: Optional[List[str]] = None,
        baseline_policy_name: str = "policy_1",
        new_op_interval: int = -1,
    ):
        """
        :param trial_settings:
        :param rl_settings:
        :param evaluation_freq: Set the frequency of evaluations in timesteps. If None, evaluation is only done with checkpoint frequency.
        :param main_id: Agent id of the main agent being trained for robustness. Other agent will be controlled by population of opponents.
        :param num_ops: Number of opponents to train against.
        :param op_experience_factor: Factor by which the opponents receive more training steps than the main agent. -1 means all opponents receive as many steps
            as the main agent in total. For f each opponent receives f times as many time steps as the main agent.
        :param baseline_artifacts: Wandb artifact ids to use as baseline for evaluation.
        :param baseline_policy_name: policy name saved in the baseline checkpoints.
        :param new_op_interval: Interval in which new opponents are added to the population. If -1, new opponents are not added.
        """
        super().__init__(trial_settings, rl_settings)
        self.evaluation_freq = evaluation_freq
        if self.evaluation_freq is None:
            self.evaluation_freq = 1

        self.main_id = 0
        self.opponent_id = 1

        self.num_ops = num_ops
        # self.main_iter_steps = main_steps

        self.baseline_artifacts = baseline_artifacts

        # Calculate what is the factor of iterations that have to be performed more for opponents compared to the main agent
        # Iterations scale with the number of opponents. If op_expericence_factor os -1, set to imbalanced mode where all opponents have together get as many
        # timesteps as the main agent alone
        self.op_iteration_factor = int(
            op_experience_factor * self.num_ops if op_experience_factor > 0 else 1
        )

        self.eval_agents = {"gen": []}
        self.main_policy = "main"

        self.opponent_policies = [f"op_{i}" for i in range(self.num_ops)]

        op_fraction = 1
        self.num_ops_per_iteration = int(len(self.opponent_policies) * op_fraction)
        if self.num_ops_per_iteration <= 0:  # Always train at least one op
            self.num_ops_per_iteration = 1

        self.baseline_policy_name = baseline_policy_name

        self.agent_infos = {
            "num_agents": self.num_ops,
            "opponent_timesteps": [0] * self.num_ops,
            "deactivated": [False] * self.num_ops,
            "opponent_policies": self.opponent_policies,
        }

        self.main_agent_id = None
        self.op_agent_id = None

        self.new_op_interval = new_op_interval

        self.policy_episode_num = defaultdict(int)

        self.currently_training = []

        self.policy_cache = self.trial.out_path / "policy_cache"

        # Add baseline policy to eval agents
        if self.baseline_artifacts is not None:
            self.eval_agents["baselines"] = [[]]
            for baseline_artifact in self.baseline_artifacts:
                file = self.artifact_manager.get_remote_checkpoint(baseline_artifact)

                baseline_weights = load_saved_weights(
                    file,
                    self.scenario_name,
                    self.baseline_policy_name,
                    self.trainer_class,
                )
                self.eval_agents["baselines"][0].append(baseline_weights)

    def set_up_config(self):
        """Config setup for PBT."""
        # Perform config setup of base manager
        super().set_up_config()

        # Perform config setup for eval
        self.config.update(aprl_defense.configs.eval.online_eval)
        # Apply conditional eval settings
        self.config.update(
            {
                "custom_eval_function": create_pbt_eval_func(self.eval_agents),
                # Set evaluation interval to enable evaluation
                # The interval is with respect to training itereations, i.e. calls of trainer.train()
                # Currently the `checkpoint_freq` also determines the evaluation interval
                # Consequently, num_ops + 1 intervals equal 1 training iteration for main, i.e. main trains batch_size steps
                "evaluation_interval": (self.op_iteration_factor + 1)
                * max(1 // 50000, 1),
            }
        )

        action_spaces, observation_spaces, agent_ids = spaces_from_env(self.env)
        self.main_agent_id = agent_ids[0]
        self.op_agent_id = agent_ids[1]

        policies = {}
        for policy in [self.main_policy] + self.opponent_policies + ["eval_op"]:
            agent_id = (
                self.main_agent_id if policy == self.main_policy else self.op_agent_id
            )
            obs_space = observation_spaces[agent_id]
            act_space = action_spaces[agent_id]
            policies[policy] = (
                None,
                obs_space,
                act_space,
                {
                    "agent_id": agent_id,
                },
            )
        # Apparently fields don't work but variables do in this closure. Values are constant anyway, so it doesn't matter
        main_agent_id = self.main_agent_id
        main_policy = self.main_policy

        def policy_mapping_fn_eval(agent_id, episode, **kwargs):
            if agent_id == main_agent_id:
                return main_policy
            else:  # The agent that is controlled by one of the opponent agents
                return "eval_op"

        if self.rl.limit_policy_cache:
            # We reduce main memory usage by setting this lower. Each worker might have up to this number of policies in main memory. Because we distribute the
            # opponents to the workers deterministically, we only need a subset of all opponents at each worker
            worker_policy_map_capacity = (
                max(1, self.num_ops // self.config["num_workers"]) + 2
            )  # + 2 for eval_op and main
        else:
            worker_policy_map_capacity = 100

        logger.info(f"Creating policy cache folder at {self.policy_cache}")
        self.policy_cache.mkdir(parents=True, exist_ok=True)

        multiagent = {
            "policies": policies,
            "policy_map_capacity": worker_policy_map_capacity,
            "policy_mapping_fn": None,
            "policy_map_cache": str(self.policy_cache),
            # I'm not 100% positive but it seems like only policies which were declared trainable in this config settings at time of initializing the
            # trainer can actually be trained. Even if we change this setting later online during training, only a subset of these policies provided
            # here will be updated. As such it is important that this setting is set correctly and it can't be corrected by chaning this setting
            # online.
            "policies_to_train": [main_policy] + self.opponent_policies,
        }

        if "env_config" not in self.config:
            self.config["env_config"] = {}
        self.config["env_config"]: dict
        self.config["env_config"]["scenario_name"] = self.scenario_name
        self.config["multiagent"] = multiagent

        # Update eval config
        self.config["evaluation_config"] = {}
        self.config["evaluation_config"]["multiagent"] = deepcopy(multiagent)
        self.config["evaluation_config"]["multiagent"][
            "policy_mapping_fn"
        ] = policy_mapping_fn_eval

        # self.config["evaluation_config"]["rollout_fragment_length"] = 25

    def set_up_trainer(self) -> None:
        """Set up trainer for PBT."""

        if self.trial.continue_artifact is None:
            # Create a new trainer
            self.trainer = create_trainer(self.trainer_class, self.config)
        else:
            # Use saved trainer from checkpoint
            file = self.artifact_manager.get_remote_checkpoint(
                self.trial.continue_artifact
            )
            if self.trial.override_config:
                config = self.config
            else:
                config = None
            self.trainer = load_saved_checkpoint(self.trainer_class, file, config)

    def start_training_loop(self):
        """PBT training loop. Opponents are assigned to specific workers, this way not
        all workers need to have all policies, which reduces the necessary memory.
        Currently, the number of workers must be smaller or equal to the number of
        opponents, i.e. having an opponent on multiple workers is not
        supported."""

        if self.rl.limit_policy_cache:
            # Special policy map setup for more memory efficient PBT on many workers
            # All workers are assigned a low policy map capacity. However, we want to
            # undo this for the main driver which should contain all policies in RAM
            driver_policy_capacity = self.num_ops + 2  # + 2 for eval_op and main

            def increase_driver_policy_capacity(worker):
                if worker.worker_index == 0:  # Driver is at index 0 in RLlib
                    worker.policy_map.deque = deque(maxlen=driver_policy_capacity)
                    # TODO copy from original deque
                    for item in worker.policy_map.cache:
                        worker.policy_map.deque.append(item)

            self.trainer.workers.foreach_worker(increase_driver_policy_capacity)

        logger.info("Running PBT")
        # save_params(self.config, self.trial.out_path)

        worker_id_to_opponent_pols = self._distribute_ops_to_workers()
        self._set_mapping_fns(worker_id_to_opponent_pols)

        timesteps_main = 0
        timesteps_total = 0
        next_eval_at = 1

        iterations = 0
        num_checkpoints_collected = 0
        num_ops_added = 0
        pbar = tqdm(total=self.rl.max_timesteps)
        while timesteps_main < self.rl.max_timesteps:

            # ===== OPPONENT TRAINING =====
            self._add_new_policy_if_necessary(num_ops_added, timesteps_main)

            self._to_train_opponent_training(worker_id_to_opponent_pols)
            # Train a random opponent for enough timesteps that there are on average as many timesteps as the main agent trains for each opponent
            results = self._train(iterations=self.op_iteration_factor)
            timesteps_total = results["timesteps_total"]

            # ===== MAIN TRAINING =====y
            timesteps_before = timesteps_total
            self._to_train_main_training()
            results = self._train()

            timesteps_total = results["timesteps_total"]

            main_additional = timesteps_total - timesteps_before

            timesteps_main += main_additional

            if self.main_policy not in results["policy_reward_mean"]:  # Sanity check
                raise ValueError("No result values collected. Is 'horizon' set?")
            else:
                # Log results for iteration
                for policy, value in results["policy_reward_mean"].items():
                    wandb.log(
                        {
                            policy: value,
                            "timestep": timesteps_main,
                            "timestep_agg": timesteps_total,
                        }
                    )  # Logging multiple x-axes
                for policy, num_episodes in self.policy_episode_num.items():
                    wandb.log(
                        {
                            f"num_episodes_{policy}": num_episodes,
                            "timestep": timesteps_main,
                            "timestep_agg": timesteps_total,
                        }
                    )  # Logging multiple x-axes

            # Save new checkpoint if applicable
            next_checkpoint_at = (num_checkpoints_collected + 1) * self.checkpoint_freq
            if timesteps_main > next_checkpoint_at:
                self.artifact_manager.save_new_checkpoint()

                num_checkpoints_collected += 1

            if timesteps_main > next_eval_at:
                next_eval_at += 1

                next_gen = []
                # Update stored old opponents
                for op_policy in self.opponent_policies:
                    weights = self.trainer.get_weights(op_policy)
                    weights_copy = deepcopy(weights)
                    next_gen.append(weights_copy)
                # Append the list of the next generation of opponents
                self.eval_agents["gen"].append(next_gen)

                custom_eval_log(results, timesteps_total, timesteps_main)

            iterations += 1
            pbar.update(timesteps_main - pbar.n)
        pbar.close()

        self.artifact_manager.save_new_checkpoint()
        logger.info("Saved final checkpoint")

    def _distribute_ops_to_workers(self) -> List[List[str]]:
        """Creates a list of the following form:
        worker_id, op_pols running on that worker
          index 0 -->  [op0, op1]
          index 1 -->  [op2, op3] etc."""

        num_workers = self.config["num_workers"]
        worker_id_to_opponent_pols: List[List[str]] = []

        # The total number of workers is num_workers + 1, as the driver, which does not perform any rollouts, gets assigned the index 0
        for _ in range(num_workers + 1):
            worker_id_to_opponent_pols.append([])

        if self.num_ops >= num_workers:
            for op_i, op_pol in enumerate(self.opponent_policies):
                # Distribute opponents onto workers 1 ... num_workers
                worker_id = op_i % num_workers
                worker_id_to_opponent_pols[worker_id + 1].append(op_pol)
        else:  # Fewer opponents than workers -> one opponent might train on multiple workers
            for worker_i in range(num_workers):
                op_i = worker_i % self.num_ops
                worker_id_to_opponent_pols[worker_i + 1].append(
                    self.opponent_policies[op_i]
                )

        # Worker with id 0 is main driver and is not assigned anything, as it does not perform rollouts

        return worker_id_to_opponent_pols

    def _set_mapping_fns(self, worker_id_to_opponent_pols):
        """Update the mapping functions for the workers according to the mapping of workers to opponent policies."""
        # This is necessary, as the closure apparently cannot be pickled if there are references to self.
        main_agent_id = self.main_agent_id
        main_policy = self.main_policy

        def mapping_fns(worker):
            worker_id = worker.worker_index
            ops = worker_id_to_opponent_pols[worker_id]
            map_fn = create_policy_mapping_function(main_agent_id, main_policy, ops)
            worker.set_policy_mapping_fn(map_fn)

        self.trainer.workers.foreach_worker(mapping_fns)

    def _to_train_opponent_training(self, worker_id_to_opponent_pols):
        """Set to_train and mapping functions for each worker separately, so each worker only needs access to few policies at a time. This way less policies
        have to be kept in memory"""

        self.currently_training = self.opponent_policies
        # To allow for pickling
        opponent_policies = self.opponent_policies

        # Set policies_to_train and policy_mapping_fn according to this setup
        def to_train(worker):
            worker_id = worker.worker_index
            if worker_id != 0:
                worker.set_is_policy_to_train(worker_id_to_opponent_pols[worker_id])
            else:
                worker.set_is_policy_to_train(opponent_policies)  # Train all

        self.trainer.workers.foreach_worker(to_train)

        # Sync before training in case there were changes before
        self.trainer.workers.sync_weights(policies=[self.main_policy])

    def _to_train_main_training(self):
        """Set the list of policies to train for training the main policy."""

        self.currently_training = [self.main_policy]
        main_policy = self.main_policy
        # Enable training of given policy
        self.trainer.workers.foreach_worker(
            lambda worker: worker.set_is_policy_to_train([main_policy])
        )

        # Sync before training in case there were changes before
        self.trainer.workers.sync_weights(policies=[self.main_policy])

    def _train(self, iterations: int = 1):
        """Setup for training and perform training with given number of iterations. Setup consists of setting which policy to train, setting given
        policy_map_fn, syncing the weights."""

        if iterations < 1:
            raise ValueError(
                f"Train for at least 1 iterations, iterations was set to {iterations}"
            )

        # Train main agent for given number of iterations
        for i in range(iterations):
            time_start = time.time()
            results = self.trainer.train()
            self._collect_stats(results)
            print(f"Iteration: {i} took {time.time()-time_start}s")

        return results

    def _add_new_policy_if_necessary(self, num_ops_added, timesteps_main):
        """Add new opponent policy if the interval has been reached. If self.new_op_interval is set to -1 never add new policies."""

        if (
            self.new_op_interval > -1
            and timesteps_main > (num_ops_added + 1) * self.new_op_interval
        ):
            # If I understand correctly, when adding a new policy for non-standard environments RLlib can't automatically determine obs and action space
            # This is why we manually determin and apply them here
            action_spaces, observation_spaces, agent_ids = spaces_from_env(self.env)
            new_pol_id = f"op_{self.num_ops}"
            self.num_ops += 1
            num_ops_added += 1

            # This policy is for a new opponent, so spaces will be determined by opponent_id
            _ = self.trainer.add_policy(  # Return value is the new policy
                policy_id=new_pol_id,
                policy_cls=type(self.trainer.get_policy(self.opponent_policies[-1])),
                observation_space=observation_spaces[self.op_agent_id],
                action_space=action_spaces[self.op_agent_id],
            )

            # Track the new policy as one of the active policies
            self.opponent_policies.append(new_pol_id)

    def _collect_stats(self, results: dict):
        for pol in self.opponent_policies + [self.main_policy]:
            if pol in self.currently_training:
                key = f"policy_{pol}_reward"
                if key in results["hist_stats"]:
                    num_episodes = len(
                        results["hist_stats"][key]
                    )  # AFAICT this is the only way to get the number of episodes per policy
                    self.policy_episode_num[pol] += num_episodes

    def get_mode(self):
        return "pbt"
