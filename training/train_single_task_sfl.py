# Adapted from PureJaxRL implementation and minigrid baselines, source:
# https://github.com/lupuandr/explainable-policies/blob/50acbd777dc7c6d6b8b7255cd1249e81715bcb54/purejaxrl/ppo_rnn.py#L4
# https://github.com/lcswillems/rl-starter-files/blob/master/model.py
import time
from dataclasses import asdict, dataclass
from functools import partial
from typing import Optional

import jax
import jax.numpy as jnp
import jax.tree_util as jtu
import optax
import pyrallis
import wandb
import xminigrid
from flax.jax_utils import replicate, unreplicate
from flax.training.train_state import TrainState
from nn import ActorCriticRNN
from utils import Transition, calculate_gae, ppo_update_networks, rollout, rollout_nsteps, rollout_eval
from xminigrid.environment import Environment, EnvParams
from xminigrid.wrappers import GymAutoResetWrapper, UEDAutoResetWrapper

# this will be default in new jax versions anyway
jax.config.update("jax_threefry_partitionable", True)


@dataclass
class TrainConfig:
    project: str = "xminigrid-single"
    group: str = "BlockedUnlockPickUp-DR"
    mode: str = "online"
    name: str = "single-task-ppo"
    env_id: str = "MiniGrid-BlockedUnlockPickUp"
    benchmark_id: Optional[str] = None
    ruleset_id: Optional[int] = None
    img_obs: bool = False
    # agent
    action_emb_dim: int = 16
    rnn_hidden_dim: int = 1024
    rnn_num_layers: int = 1
    head_hidden_dim: int = 256
    # training
    num_envs: int = 8192
    num_steps: int = 16
    update_epochs: int = 1
    num_minibatches: int = 16
    total_timesteps: int = 5e8
    lr: float = 0.001
    clip_eps: float = 0.2
    gamma: float = 0.99
    gae_lambda: float = 0.95
    ent_coef: float = 0.01
    vf_coef: float = 0.5
    max_grad_norm: float = 0.5
    eval_episodes: int = 80
    seed: int = 42
    eval_seed: int = 24
    # sfl
    sfl_rollout_factor: int = 10  # how many times more steps to rollout than the max_steps
    sfl_buffer_size: int = 8192
    sfl_batch_size: int = 40000
    sfl_num_batches: int = 1
    sfl_buffer_refresh_freq: int = 1
    sfl_num_envs_to_sample: int = 8192

    def __post_init__(self):
        num_devices = jax.device_count()
        assert num_devices == 1, "Multi-device training is not supported yet."
        # splitting computation across all available devices
        self.num_envs_per_device = self.num_envs // num_devices
        self.total_timesteps_per_device = self.total_timesteps // num_devices
        self.eval_episodes_per_device = self.eval_episodes // num_devices
        assert self.num_envs % num_devices == 0
        self.num_updates = self.total_timesteps_per_device // self.num_steps // self.num_envs_per_device
        print(f"Num devices: {num_devices}, Num updates: {self.num_updates}")


def make_states(config: TrainConfig):
    # for learning rate scheduling
    def linear_schedule(count):
        frac = 1.0 - (count // (config.num_minibatches * config.update_epochs)) / config.num_updates
        return config.lr * frac

    # setup environment
    env, env_params = xminigrid.make(config.env_id)
    
    eval_env = UEDAutoResetWrapper(env)
    eval_rng = jax.random.PRNGKey(config.eval_seed)
    eval_rngs = jax.random.split(eval_rng, config.eval_episodes_per_device)
    eval_init_timesteps = jax.vmap(eval_env.reset, in_axes=(None, 0))(env_params, eval_rngs)
    
    env = GymAutoResetWrapper(env)

    # for single-task XLand environments
    if config.benchmark_id is not None:
        assert "XLand-MiniGrid" in config.env_id, "Benchmarks should be used only with XLand environments."
        assert config.ruleset_id is not None, "Ruleset ID should be specified for benchmarks usage."
        benchmark = xminigrid.load_benchmark(config.benchmark_id)
        env_params = env_params.replace(ruleset=benchmark.get_ruleset(config.ruleset_id))

    # enabling image observations if needed
    if config.img_obs:
        from xminigrid.experimental.img_obs import RGBImgObservationWrapper

        env = RGBImgObservationWrapper(env)

    # setup training state
    rng = jax.random.key(config.seed)
    rng, _rng = jax.random.split(rng)

    network = ActorCriticRNN(
        num_actions=env.num_actions(env_params),
        action_emb_dim=config.action_emb_dim,
        rnn_hidden_dim=config.rnn_hidden_dim,
        rnn_num_layers=config.rnn_num_layers,
        head_hidden_dim=config.head_hidden_dim,
        img_obs=config.img_obs,
    )
    # [batch_size, seq_len, ...]
    init_obs = {
        "observation": jnp.zeros((config.num_envs_per_device, 1, *env.observation_shape(env_params))),
        "prev_action": jnp.zeros((config.num_envs_per_device, 1), dtype=jnp.int32),
        "prev_reward": jnp.zeros((config.num_envs_per_device, 1)),
    }
    init_hstate = network.initialize_carry(batch_size=config.num_envs_per_device)

    network_params = network.init(_rng, init_obs, init_hstate)
    tx = optax.chain(
        optax.clip_by_global_norm(config.max_grad_norm),
        optax.inject_hyperparams(optax.adam)(learning_rate=linear_schedule, eps=1e-8),  # eps=1e-5
    )
    train_state = TrainState.create(apply_fn=network.apply, params=network_params, tx=tx)

    return rng, env, env_params, eval_env, eval_init_timesteps, init_hstate, train_state


def make_train(
    env: Environment,
    env_params: EnvParams,
    eval_env: Environment, 
    eval_init_timesteps,
    config: TrainConfig,
):
    @partial(jax.pmap, axis_name="devices")
    def train(
        rng: jax.Array,
        train_state: TrainState,
        init_hstate: jax.Array,
    ):
        # INIT ENV
        rng, _rng = jax.random.split(rng)
        reset_rng = jax.random.split(_rng, config.num_envs_per_device)

        timestep = jax.vmap(env.reset, in_axes=(None, 0))(env_params, reset_rng)
        prev_action = jnp.zeros(config.num_envs_per_device, dtype=jnp.int32)
        prev_reward = jnp.zeros(config.num_envs_per_device)

        # TRAIN LOOP
        def _update_step(runner_state, _):
            # COLLECT TRAJECTORIES
            def _env_step(runner_state, _):
                rng, train_state, prev_timestep, prev_action, prev_reward, prev_hstate = runner_state

                # SELECT ACTION
                rng, _rng = jax.random.split(rng)
                dist, value, hstate = train_state.apply_fn(
                    train_state.params,
                    {
                        # [batch_size, seq_len=1, ...]
                        "observation": prev_timestep.observation[:, None],
                        "prev_action": prev_action[:, None],
                        "prev_reward": prev_reward[:, None],
                    },
                    prev_hstate,
                )
                action, log_prob = dist.sample_and_log_prob(seed=_rng)
                # squeeze seq_len where possible
                action, value, log_prob = action.squeeze(1), value.squeeze(1), log_prob.squeeze(1)

                # STEP ENV
                timestep = jax.vmap(env.step, in_axes=(None, 0, 0))(env_params, prev_timestep, action)
                transition = Transition(
                    done=timestep.last(),
                    action=action,
                    value=value,
                    reward=timestep.reward,
                    log_prob=log_prob,
                    obs=prev_timestep.observation,
                    prev_action=prev_action,
                    prev_reward=prev_reward,
                )
                runner_state = (rng, train_state, timestep, action, timestep.reward, hstate)
                return runner_state, transition

            initial_hstate = runner_state[-1]
            # transitions: [seq_len, batch_size, ...]
            runner_state, transitions = jax.lax.scan(_env_step, runner_state, None, config.num_steps)

            # CALCULATE ADVANTAGE
            rng, train_state, timestep, prev_action, prev_reward, hstate = runner_state
            # calculate value of the last step for bootstrapping
            _, last_val, _ = train_state.apply_fn(
                train_state.params,
                {
                    "observation": timestep.observation[:, None],
                    "prev_action": prev_action[:, None],
                    "prev_reward": prev_reward[:, None],
                },
                hstate,
            )
            advantages, targets = calculate_gae(transitions, last_val.squeeze(1), config.gamma, config.gae_lambda)

            # UPDATE NETWORK
            def _update_epoch(update_state, _):
                def _update_minbatch(train_state, batch_info):
                    init_hstate, transitions, advantages, targets = batch_info
                    new_train_state, update_info = ppo_update_networks(
                        train_state=train_state,
                        transitions=transitions,
                        init_hstate=init_hstate.squeeze(1),
                        advantages=advantages,
                        targets=targets,
                        clip_eps=config.clip_eps,
                        vf_coef=config.vf_coef,
                        ent_coef=config.ent_coef,
                    )
                    return new_train_state, update_info

                rng, train_state, init_hstate, transitions, advantages, targets = update_state

                # MINIBATCHES PREPARATION
                rng, _rng = jax.random.split(rng)
                permutation = jax.random.permutation(_rng, config.num_envs_per_device)
                # [seq_len, batch_size, ...]
                batch = (init_hstate, transitions, advantages, targets)
                # [batch_size, seq_len, ...], as our model assumes
                batch = jtu.tree_map(lambda x: x.swapaxes(0, 1), batch)

                shuffled_batch = jtu.tree_map(lambda x: jnp.take(x, permutation, axis=0), batch)
                # [num_minibatches, minibatch_size, ...]
                minibatches = jtu.tree_map(
                    lambda x: jnp.reshape(x, (config.num_minibatches, -1) + x.shape[1:]), shuffled_batch
                )
                train_state, update_info = jax.lax.scan(_update_minbatch, train_state, minibatches)

                update_state = (rng, train_state, init_hstate, transitions, advantages, targets)
                return update_state, update_info

            # [seq_len, batch_size, num_layers, hidden_dim]
            init_hstate = initial_hstate[None, :]
            update_state = (rng, train_state, init_hstate, transitions, advantages, targets)
            update_state, loss_info = jax.lax.scan(_update_epoch, update_state, None, config.update_epochs)

            # averaging over minibatches then over epochs
            loss_info = jtu.tree_map(lambda x: x.mean(-1).mean(-1), loss_info)

            rng, train_state = update_state[:2]
            # EVALUATE AGENT
            rng, _rng = jax.random.split(rng)
            eval_rng = jax.random.split(_rng, num=config.eval_episodes_per_device)

            # vmap only on rngs
            eval_stats = jax.vmap(rollout_eval, in_axes=(0, None, None, 0, None, None, None))(
                eval_rng,
                eval_env,
                env_params,
                eval_init_timesteps,
                train_state,
                # TODO: make this as a static method mb?
                jnp.zeros((1, config.rnn_num_layers, config.rnn_hidden_dim)),
                1,
            )
            eval_stats = jax.lax.pmean(eval_stats, axis_name="devices")
            loss_info.update(
                {
                    "eval/returns": eval_stats.reward.mean(0),
                    "eval/lengths": eval_stats.length.mean(0),
                    "eval/success_rate": jnp.mean(eval_stats.success/eval_stats.episodes),
                    "lr": train_state.opt_state[-1].hyperparams["learning_rate"],
                }
            )
            
            def _callback(info):
                wandb.log(
                    info,
                    # step=info["update_step"]
                )
            
            jax.experimental.io_callback(_callback, None, loss_info)
            
            runner_state = (rng, train_state, timestep, prev_action, prev_reward, hstate)
            return runner_state, loss_info

        runner_state = (rng, train_state, timestep, prev_action, prev_reward, init_hstate)
        runner_state, loss_info = jax.lax.scan(_update_step, runner_state, None, config.num_updates)
        return {"runner_state": runner_state, "loss_info": loss_info}

    return train


@pyrallis.wrap()
def train(config: TrainConfig):
    # logging to wandb
    run = wandb.init(
        project=config.project,
        group=config.group,
        name=config.name,
        config=asdict(config),
        save_code=True,
        mode=config.mode,
    )

    rng, env, env_params, eval_env, eval_init_timesteps, init_hstate, train_state = make_states(config)
    # replicating args across devices
    rng = jax.random.split(rng, num=jax.local_device_count())
    train_state = replicate(train_state, jax.local_devices())
    init_hstate = replicate(init_hstate, jax.local_devices())

    print("Compiling...")
    t = time.time()
    train_fn = make_train(env, env_params, eval_env, eval_init_timesteps, config)
    train_fn = train_fn.lower(rng, train_state, init_hstate).compile()
    elapsed_time = time.time() - t
    print(f"Done in {elapsed_time:.2f}s.")

    print("Training...")
    t = time.time()
    train_info = jax.block_until_ready(train_fn(rng, train_state, init_hstate))
    elapsed_time = time.time() - t
    print(f"Done in {elapsed_time:.2f}s")

    print("Logging...")
    loss_info = unreplicate(train_info["loss_info"])

    total_transitions = 0
    for i in range(config.num_updates):
        # summing total transitions per update from all devices
        total_transitions += config.num_steps * config.num_envs_per_device * jax.local_device_count()
        info = jtu.tree_map(lambda x: x[i].item(), loss_info)
        info["transitions"] = total_transitions
        wandb.log(info)

    run.summary["training_time"] = elapsed_time
    run.summary["steps_per_second"] = (config.total_timesteps_per_device * jax.local_device_count()) / elapsed_time

    print("Final return: ", float(loss_info["eval/returns"][-1]))
    run.finish()


if __name__ == "__main__":
    train()