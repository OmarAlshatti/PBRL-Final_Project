# Defenses Against Adversarial Policies

## Setup and Requirements on Main OS

Should work with python 3.7, 3.8

Ubuntu 20.0.4

Cuda and CudNN

Docker CE (Docker Desktop will not work)

Docker CE setup with non-Root priviliges

### Installation example

Install using Docker. (Ideally through VS Code)

Then Build and Open Devcontainer


Run on terminal to access container
```
docker exec -it <Docker Image ID> bash

```



Run below code in docker container to setup wandb in Docker Container.

```
python -c "import wandb; wandb.login(key='your-api-key')"

```


## Running Training

To change the output path change `TrialSettings.out_path` via gin-config.
This can be overwritten with the environment variable `POLICY_DEFENSE_OUT`.

### Configuration

Most frequently used settings can be changed via gin.  
The settings intended to be configured with gin are:
- `TrialSettings` (`aprl_defense.trial.settings.TrialSettings`)
- `RLSettings` (`aprl_defense.trial.settings.RLSettings`)
- Additionally, depending on whether one of these modes is used
    - `selfplay` (`aprl_defense.training_managers.simple_training_manager.SelfplayTrainingManager`)
    - `single-agent` - no additonal arguments
    - `attack` (`aprl_defense.training_managers.simple_training_manager.AttackManager`)
    - `pbt` (`aprl_defense.training_managers.pbt_manager.PBTManager`)


#### PBT With Subsequent Attack

Runs several PBT jobs, subsequently attecks each seed. (using laser tag as example)

```bash
python -m aprl_defense.train \
  -f "gin/icml/pbt/laser_tag.gin" \
  -p "TrialSettings.mode='pbt+attack'" \
  -p "TrialSettings.wandb_group = <name for group of experiments>" \
  -p "RLSettings.env = 'mpe_simple_push'" \
  -p "pbt_train_attack.num_ops_list=[2, 4, 8, 16]" \
  -p "pbt_train_attack.num_training=5" \
  -p "pbt_train_attack.num_attacks=5" \
  -p "pbt_train_attack.num_processes=4"
```


## Some Explanations

In all but the most basic setups creating an RLlib config for multiagent training requires programmatically creating a config in python and
these configs could not be created simply by passing in a config file.
For convenience the most commonly changed hyperparameters and set-up configurations can be changed with gin, additional modifications can be
performed by overriding the RLlib config.
