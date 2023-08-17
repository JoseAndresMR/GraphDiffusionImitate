"""
Usage:
python test_stochgpmp.py
"""
import logging
import pathlib
import random
import time
from typing import Optional
import hydra
import numpy as np
import pybullet as p
import torch
from omegaconf import DictConfig, OmegaConf

from robot_envs.pybullet.utils import random_init_static_sphere
from torch_kinematics_tree.geometrics.spatial_vector import x_rot, y_rot, z_rot
from torch_kinematics_tree.models.robot_tree import DifferentiableTree

from imitation.env.pybullet.se2_envs.robot_se2_pickplace import SE2BotPickPlace
from imitation.utils.stochgpmp import StochGPMPSE2Wrapper, plot_trajectory

class DifferentiableSE2(DifferentiableTree):
    def __init__(self, link_list: Optional[str] = None, device='cpu'):
        robot_file = "./assets/robot/se2_bot_description/robot/robot.urdf"
        robot_file = pathlib.Path(robot_file)
        self.model_path = robot_file.as_posix()
        self.name = "differentiable_2_link_planar"
        super().__init__(self.model_path, self.name, link_list=link_list, device=device)


log = logging.getLogger(__name__)


@hydra.main(
        version_base=None,
        config_path=str(pathlib.Path(__file__).parent.joinpath('imitation','config')), 
        config_name="stochgpmp_se2"
        )
def generate(cfg: DictConfig):
    log.info(OmegaConf.to_yaml(cfg))
    log.info("Running trajectories from StochGPMP...")

    # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device('cpu')
    tensor_args = {'device': device, 'dtype': torch.float32}


    seed = int(time.time())
    num_particles_per_goal = cfg.num_particles_per_goal
    num_samples = cfg.num_samples
    num_obst = cfg.num_obst
    traj_len = cfg.traj_len
    dt = cfg.dt
    obstacle_spheres = np.array(cfg.obstacles)

    # set seed
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    
    env = SE2BotPickPlace(objects_list=['cube' for i in range((obstacle_spheres.shape[1]))],
                          obj_poses=[[obstacle_spheres[0][i,:3], [0,0,0,1]] for i in range(obstacle_spheres.shape[1])])
    

    env.setControlMode("position")

    # FK
    robot_fk = DifferentiableSE2(device=device)
    

    # start state from config
    start_pose = torch.tensor(cfg.start_pose, **tensor_args)
    start_quat = torch.tensor(cfg.start_quat, **tensor_args)
    start_joints = p.calculateInverseKinematics(env.robot,
                                            env.JOINT_ID[-1],
                                            start_pose, 
                                            start_quat)[:env.dof]
    start_joints = torch.tensor(start_joints, **tensor_args)
    
    env.reset(start_joints)

    # start state from simulation 
    start_q = torch.tensor(env.getJointStates()[0],**tensor_args)
    start_state = torch.cat((start_q, torch.zeros_like(start_q)))

    # print info about the robot
    log.info("Environment info:")
    log.info(f"Robot with {env.dof} DOF, control mode: {env.control_mode}")
    log.info(f"Robot joint IDs: {env.JOINT_ID}")


    # world setup (target_pos & target_rot can be randomized)
    target_pos = np.array(cfg.target_pose)
    target_rot = (z_rot(-torch.tensor(torch.pi)) @ y_rot(-torch.tensor(torch.pi))).to(**tensor_args)
    
    planner = StochGPMPSE2Wrapper(
        env,
        robot_fk,
        start_state,
        tensor_args,
        seed,
        cfg.stochgpmp_params
    )

    log.info(planner)
    pos, vel = planner.plan_stochgpmp(
        target_pos=target_pos,
        target_rot=target_rot,
        num_particles_per_goal=num_particles_per_goal,
        num_samples=num_samples,
        traj_len=traj_len,
        dt=dt,
        obstacle_spheres=obstacle_spheres,
        opt_iters=cfg.opt_iters,
        sigma_self=cfg.sigma_self,
        sigma_coll=cfg.sigma_coll,
        sigma_goal=cfg.sigma_goal,
        sigma_goal_prior=cfg.sigma_goal_prior,
        sigma_start=cfg.sigma_start,
        sigma_gp=cfg.sigma_gp,
    )

    # Plotting
    start_q = start_state.detach().cpu().numpy()
    env.step(start_q)
    trajs = pos.detach()

    for traj in trajs:
        log.info("Restarting position")
        env.reset(start_joints)
        time.sleep(0.2)
        traj = traj.mean(dim=0)
        for t in range(traj.shape[0] - 1):
            for i in range(10):
                env.step(traj[t])
                time.sleep(0.01)
            time.sleep(dt)

        # final position
        for i in range (100):
            env.step(traj[-1])
            time.sleep(0.01)
        time.sleep(1)
        plot_trajectory(
            robot_fk,
            start_q,
            traj,
            target_pos,
            obstacle_spheres
        )

        

if __name__ == "__main__":
    generate()