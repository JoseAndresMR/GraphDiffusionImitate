import time
from typing import Union
import matplotlib.pyplot as plt
import numpy as np
import pybullet as p
import torch
from torch_kinematics_tree.geometrics.frame import Frame
from torch_planning_objectives.fields.distance_fields import (
    EESE3DistanceField, FloorDistanceField, LinkDistanceField,
    LinkSelfDistanceField)
from torch_kinematics_tree.geometrics.skeleton import get_skeleton_from_model
from tqdm.auto import tqdm

from stoch_gpmp.costs.cost_functions import (CostCollision, CostComposite,
                                             CostGoal, CostGoalPrior, CostGP)
from stoch_gpmp.planner import StochGPMP


def plot_trajectory(robot_fk,
         start_q,
         traj,
         target_pos,
         obstacle_spheres):
    plt.figure()
    ax = plt.axes(projection='3d')
    skeleton = get_skeleton_from_model(robot_fk, start_q, robot_fk.get_link_names()) # visualize IK solution
    skeleton.draw_skeleton(color='r', ax=ax)
    for t in range(traj.shape[0] - 1):
        if t % 4 == 0:
            skeleton = get_skeleton_from_model(robot_fk, traj[t], robot_fk.get_link_names())
            skeleton.draw_skeleton(color='b', ax=ax)
        
    skeleton = get_skeleton_from_model(robot_fk, traj[-1], robot_fk.get_link_names())
    skeleton.draw_skeleton(color='g', ax=ax)
    ax.plot(target_pos[0], target_pos[1], target_pos[2], 'r*', markersize=7)
    ax.scatter(obstacle_spheres[0, :, 0], obstacle_spheres[0, :, 1], obstacle_spheres[0, :, 2], s=obstacle_spheres[0, :, 3]*2000, color='r')
    plt.show()

def plan_stochgpmp(env,
                   robot_fk,
                    start_state,
                    target_pos,
                    target_rot,
                    num_particles_per_goal,
                    num_samples,
                    tensor_args,
                    traj_len,
                    dt,
                    obstacle_spheres,
                    seed):
    """
    Generate trajectories using stochgpmp
    """

    device = tensor_args['device']

    target_frame = Frame(rot=target_rot, trans=torch.from_numpy(target_pos).to(**tensor_args), device=device)
    target_quat = target_frame.get_quaternion().squeeze().cpu().numpy()  # [x, y, z, w]
    target_H = target_frame.get_transform_matrix()  # set translation and orientation of target here


    q_goal = p.calculateInverseKinematics(env.robot,
                                          env.JOINT_ID[-1],
                                          target_pos, 
                                          target_quat)[:env.dof]
    
    q_goal = torch.tensor(q_goal, **tensor_args)
    multi_goal_states = torch.cat([q_goal, torch.zeros_like(q_goal)]).unsqueeze(0)  # put IK solution

    # Cost functions
    robot_self_link = LinkSelfDistanceField(margin=0.03)
    robot_collision_link = LinkDistanceField()
    robot_goal = EESE3DistanceField(target_H)

    # Factored Cost params
    prior_sigmas = dict(
        sigma_start=0.0001,
        sigma_gp=0.0007,
    )
    sigma_self = 0.0001
    sigma_coll = 10
    sigma_goal = 0.00007
    sigma_goal_prior = 0.0001
    # Construct cost function
    cost_prior = CostGP(
        env.dof, traj_len, start_state, dt,
        prior_sigmas, tensor_args
    )
    cost_self = CostCollision(env.dof, traj_len, field=robot_self_link, sigma_coll=sigma_self)
    cost_coll = CostCollision(env.dof, traj_len, field=robot_collision_link, sigma_coll=sigma_coll)
    cost_goal = CostGoal(env.dof, traj_len, field=robot_goal, sigma_goal=sigma_goal)
    cost_goal_prior = CostGoalPrior(env.dof, traj_len, multi_goal_states=multi_goal_states, 
                                    num_particles_per_goal=num_particles_per_goal, 
                                    num_samples=num_samples, 
                                    sigma_goal_prior=sigma_goal_prior,
                                    tensor_args=tensor_args)
    cost_func_list = [cost_prior, cost_goal_prior, cost_self, cost_coll, cost_goal]
    cost_composite = CostComposite(env.dof, traj_len, cost_func_list, FK=robot_fk.compute_forward_kinematics_all_links)
    ## Planner - 2D point particle dynamics
    stochgpmp_params = dict(
        num_particles_per_goal=num_particles_per_goal,
        num_samples=num_samples,
        traj_len=traj_len,
        dt=dt,
        n_dof=env.dof,
        opt_iters=1, # Keep this 1 for visualization
        temperature=1.,
        start_state=start_state,
        multi_goal_states=multi_goal_states,
        cost=cost_composite,
        step_size=0.2,
        sigma_start_init=0.0001,
        sigma_goal_init=0.1,
        sigma_gp_init=0.1,
        sigma_start_sample=0.0001,
        sigma_goal_sample=0.07,
        sigma_gp_sample=0.02,
        seed=seed,
        tensor_args=tensor_args,
    )
    planner = StochGPMP(**stochgpmp_params)
    obstacle_spheres = torch.from_numpy(obstacle_spheres).to(**tensor_args)

    obs = {
        'obstacle_spheres': obstacle_spheres
    }

    #---------------------------------------------------------------------------
    # Optimize
    opt_iters =  100

    with tqdm(range(opt_iters + 1), desc='Optimization Step', leave=False, ) as tstep:
        for i in tstep:
            time_start = time.time()
            planner.optimize(**obs)
            pos, vel = planner.get_recent_samples()

    return pos, vel