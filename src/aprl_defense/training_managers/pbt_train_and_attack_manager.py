# type: ignore
# Ignore typing for now, I might delete this file
import time
from multiprocessing import Process, Queue
from typing import Optional, List, Union, Tuple, cast
from dataclasses import dataclass, field
from pathlib import Path

import datetime
import gin

from aprl_defense.training_managers.base_training_manager import BaseTrainingManager
from aprl_defense.training_managers.pbt_manager import PBTManager
from aprl_defense.training_managers.attack_manager import AttackManager
from aprl_defense.trial.settings import TrialSettings, RLSettings


def _start_next_process(i, process_queue, running_processes):
    new_p = process_queue.pop(0)
    new_p["process"].start()
    running_processes[i] = new_p


@gin.configurable(name_or_fn="pbt_train_attack")
class PBTTrainAndAttackManager(BaseTrainingManager):
    def __init__(
        self,
        trial_settings: TrialSettings,
        rl_settings: RLSettings,
        num_ops_list: List[int],
        num_training: int,
        num_attacks: int,
        num_processes: int,
        override: Optional[str] = None,
        override_f: List[str] = field(default_factory=list),
        both_agents: bool = False,
        op_experience_factor: float = 1.0,
    ):
        # For this type of multi-job run I would like to have all runs even with the same group name in separate groups, this is why I have a timestamp in
        # addition to the original name as the group name
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y-%m-%d-%H-%M-%S")
        trial_settings.wandb_group += "pbt+attack-" + timestamp
        self.trial_settings = trial_settings
        self.rl_settings = rl_settings
        self.num_ops_list = num_ops_list
        self.num_training = num_training
        self.num_attacks = num_attacks
        self.num_processes = num_processes
        self.both_agents = both_agents
        self.op_experience_factor = op_experience_factor
        self.override = override
        self.override_f = override_f

    def train(self):
        if self.both_agents:
            agents = [0, 1]
        else:
            agents = [0]

        def pbt_training(main_id: int, num_ops: int, q: Queue):
            training_manager = PBTManager(
                self.trial_settings,
                self.rl_settings,
                self.override,
                self.override_f,
                main_id,
                num_ops,
            )
            q.put(training_manager.wandb_id)
            q.put(training_manager.main_policy)
            training_manager.train()

        def attack(adversary_id: int, victim_policy_name: str, victim_artifact: str):
            training_manager = AttackManager(
                self.trial_settings,
                self.rl_settings,
                victim_artifact,
                adversary_id,
                victim_policy_name,
            )
            training_manager.train()

        process_queue: List[dict] = []
        running_processes: List[dict] = []
        # PBT training processes
        for it in range(self.num_training):
            for agent in agents:
                for num_ops in self.num_ops_list:
                    q = Queue()
                    p = Process(
                        target=pbt_training,
                        args=(agent, num_ops, q),
                    )
                    process_container = {
                        "type": "training",
                        "process": p,
                        "queue": q,
                        "agent": agent,
                    }
                    process_queue.append(process_container)
        for i in range(self.num_processes):
            element = process_queue.pop(0)
            element["process"].start()
            running_processes.append(element)

        while True:
            time.sleep(0.1)
            for i in range(len(running_processes)):
                element = running_processes[i]
                if element is None:
                    if len(process_queue) > 0:
                        # Simply start new process
                        _start_next_process(i, process_queue, running_processes)
                elif element["type"] == "training":  # Training process
                    p, agent, q = element["process"], element["agent"], element["queue"]
                    p.join(timeout=0)
                    if p.is_alive():
                        pass  # Process is still going
                    else:  # Training process finished -> add attackers, start new process
                        wandb_id = q.get()
                        main_policy_name = q.get()
                        print(f"{wandb_id} is finished")

                        # Add attackers to just finished process to queue
                        attacker_agent_id = 1 - agent
                        for attack_it in range(self.num_attacks):
                            p = Process(
                                target=attack,
                                args=(
                                    attacker_agent_id,
                                    main_policy_name,
                                    wandb_id + ":latest",
                                ),
                            )
                            process_container = {"type": "attack", "process": p}
                            process_queue.append(process_container)

                        element["process"].terminate()
                        element["process"] = None
                        # Start appropriate number of attacks on this artifact
                        _start_next_process(i, process_queue, running_processes)
                else:  # Attack process -> simply get next from queue (if just finished
                    p = element["process"]
                    p.join(timeout=0)
                    if p.is_alive():
                        pass  # Process is still going
                    else:  # Attack process finished -> start new process from queue
                        if len(process_queue) > 0:
                            element["process"].terminate()
                            element["process"] = None
                            _start_next_process(i, process_queue, running_processes)
                        else:
                            running_processes[i] = None

            # Check whether all are done
            if all([p is None for p in running_processes]) and len(process_queue) == 0:
                break
        print("All processes finished!")
