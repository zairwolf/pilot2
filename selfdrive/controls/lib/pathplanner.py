import os
import math
from common.realtime import sec_since_boot, DT_MDL
from selfdrive.swaglog import cloudlog
from selfdrive.controls.lib.lateral_mpc import libmpc_py
from selfdrive.controls.lib.drive_helpers import MPC_COST_LAT
from selfdrive.controls.lib.lane_planner import LanePlanner
from selfdrive.config import Conversions as CV
from common.params import Params, put_nonblocking
import cereal.messaging as messaging
from cereal import log
# dragonpilot
from common.dp import get_last_modified
from common.numpy_fast import interp

LaneChangeState = log.PathPlan.LaneChangeState
LaneChangeDirection = log.PathPlan.LaneChangeDirection

LOG_MPC = os.environ.get('LOG_MPC', False)

LANE_CHANGE_SPEED_MIN = 45 * CV.MPH_TO_MS
LANE_CHANGE_TIME_MAX = 10.

DESIRES = {
  LaneChangeDirection.none: {
    LaneChangeState.off: log.PathPlan.Desire.none,
    LaneChangeState.preLaneChange: log.PathPlan.Desire.none,
    LaneChangeState.laneChangeStarting: log.PathPlan.Desire.none,
    LaneChangeState.laneChangeFinishing: log.PathPlan.Desire.none,
  },
  LaneChangeDirection.left: {
    LaneChangeState.off: log.PathPlan.Desire.none,
    LaneChangeState.preLaneChange: log.PathPlan.Desire.none,
    LaneChangeState.laneChangeStarting: log.PathPlan.Desire.laneChangeLeft,
    LaneChangeState.laneChangeFinishing: log.PathPlan.Desire.laneChangeLeft,
  },
  LaneChangeDirection.right: {
    LaneChangeState.off: log.PathPlan.Desire.none,
    LaneChangeState.preLaneChange: log.PathPlan.Desire.none,
    LaneChangeState.laneChangeStarting: log.PathPlan.Desire.laneChangeRight,
    LaneChangeState.laneChangeFinishing: log.PathPlan.Desire.laneChangeRight,
  },
}


def calc_states_after_delay(states, v_ego, steer_angle, curvature_factor, steer_ratio, delay):
  states[0].x = v_ego * delay
  states[0].psi = v_ego * curvature_factor * math.radians(steer_angle) / steer_ratio * delay
  return states


class PathPlanner():
  def __init__(self, CP):
    self.LP = LanePlanner()

    self.last_cloudlog_t = 0
    self.steer_rate_cost = CP.steerRateCost

    self.setup_mpc()
    self.solution_invalid_cnt = 0
    self.lane_change_enabled = Params().get('LaneChangeEnabled') == b'1'
    self.lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.lane_change_timer = 0.0
    self.lane_change_ll_prob = 1.0
    self.prev_one_blinker = False

    # dragonpilot
    self.params = Params()
    self.dragon_auto_lc_enabled = False
    self.dragon_auto_lc_allowed = False
    self.dragon_auto_lc_timer = None
    self.dragon_assisted_lc_min_mph = LANE_CHANGE_SPEED_MIN
    self.dragon_auto_lc_min_mph = 60 * CV.MPH_TO_MS
    self.dragon_auto_lc_delay = 2.
    self.last_ts = 0.
    self.dp_last_modified = None
    self.dp_enable_sr_boost = False
    self.dp_continuous_auto_lc = False
    self.dp_did_auto_lc = False

    self.dp_steer_ratio = 0.
    self.dp_sr_boost_bp = None
    self.dp_sr_boost_range = None

  def setup_mpc(self):
    self.libmpc = libmpc_py.libmpc
    self.libmpc.init(MPC_COST_LAT.PATH, MPC_COST_LAT.LANE, MPC_COST_LAT.HEADING, self.steer_rate_cost)

    self.mpc_solution = libmpc_py.ffi.new("log_t *")
    self.cur_state = libmpc_py.ffi.new("state_t *")
    self.cur_state[0].x = 0.0
    self.cur_state[0].y = 0.0
    self.cur_state[0].psi = 0.0
    self.cur_state[0].delta = 0.0

    self.angle_steers_des = 0.0
    self.angle_steers_des_mpc = 0.0
    self.angle_steers_des_prev = 0.0
    self.angle_steers_des_time = 0.0

  def update(self, sm, pm, CP, VM):
    # dragonpilot
    cur_time = sec_since_boot()
    if cur_time - self.last_ts >= 5.:
      modified = get_last_modified()
      if self.dp_last_modified != modified:
        self.lane_change_enabled = True if self.params.get("LaneChangeEnabled", encoding='utf8') == "1" else False
        if self.lane_change_enabled:
          self.dragon_auto_lc_enabled = True if self.params.get("DragonEnableAutoLC", encoding='utf8') == "1" else False
          # adjustable assisted lc min speed
          try:
            self.dragon_assisted_lc_min_mph = float(self.params.get("DragonAssistedLCMinMPH", encoding='utf8'))
          except (TypeError, ValueError):
            self.dragon_assisted_lc_min_mph = 45
          self.dragon_assisted_lc_min_mph *= CV.MPH_TO_MS
          if self.dragon_assisted_lc_min_mph < 0:
            self.dragon_assisted_lc_min_mph = 0
          if self.dragon_auto_lc_enabled:
            self.dp_continuous_auto_lc = True if self.params.get("DragonEnableContALC", encoding='utf8') == "1" else False
            # adjustable auto lc min speed
            try:
              self.dragon_auto_lc_min_mph = float(self.params.get("DragonAutoLCMinMPH", encoding='utf8'))
            except (TypeError, ValueError):
              self.dragon_auto_lc_min_mph = 60
            self.dragon_auto_lc_min_mph *= CV.MPH_TO_MS
            if self.dragon_auto_lc_min_mph < 0:
              self.dragon_auto_lc_min_mph = 0
            # when auto lc is smaller than assisted lc, we set assisted lc to the same speed as auto lc
            if self.dragon_auto_lc_min_mph < self.dragon_assisted_lc_min_mph:
              self.dragon_assisted_lc_min_mph = self.dragon_auto_lc_min_mph
            # adjustable auto lc delay
            try:
              self.dragon_auto_lc_delay = float(self.params.get("DragonAutoLCDelay", encoding='utf8'))
            except (TypeError, ValueError):
              self.dragon_auto_lc_delay = 2.
            if self.dragon_auto_lc_delay < 0:
              self.dragon_auto_lc_delay = 0
        else:
          self.dragon_auto_lc_enabled = False

        self.dp_enable_sr_boost = True if self.params.get("DragonEnableSteerBoost", encoding='utf8') == "1" else False
        if self.dp_enable_sr_boost:
          try:
            sr_boost_min = float(self.params.get("DragonSteerBoostMin", encoding='utf8'))
            sr_boost_Max = float(self.params.get("DragonSteerBoostMax", encoding='utf8'))
            self.dp_sr_boost_range = [sr_boost_min, sr_boost_Max]

            boost_min_at = float(self.params.get("DragonSteerBoostMinAt", encoding='utf8'))
            boost_max_at = float(self.params.get("DragonSteerBoostMaxAt", encoding='utf8'))
            self.dp_sr_boost_bp = [boost_min_at, boost_max_at]
          except (TypeError, ValueError):
            put_nonblocking("DragonEnableSteerBoost", '0')
            self.dp_enable_sr_boost = False
        if not self.dp_enable_sr_boost:
          self.dp_sr_boost_range = [0., 0.]
          self.dp_sr_boost_bp = [0., 0.]

        self.dp_last_modified = modified
      self.last_ts = cur_time

    v_ego = sm['carState'].vEgo
    angle_steers = sm['carState'].steeringAngle
    active = sm['controlsState'].active

    angle_offset = sm['liveParameters'].angleOffset

    # Run MPC
    self.angle_steers_des_prev = self.angle_steers_des_mpc
    VM.update_params(sm['liveParameters'].stiffnessFactor, sm['liveParameters'].steerRatio)
    curvature_factor = VM.curvature_factor(v_ego)
    boost_rate = (1 + (interp(abs(angle_steers), self.dp_sr_boost_bp, self.dp_sr_boost_range) / 100)) if self.dp_enable_sr_boost else 1
    self.dp_steer_ratio = VM.sR * boost_rate

    self.LP.parse_model(sm['model'])

    # Lane change logic
    one_blinker = sm['carState'].leftBlinker != sm['carState'].rightBlinker
    below_lane_change_speed = v_ego < self.dragon_assisted_lc_min_mph

    if sm['carState'].leftBlinker:
      self.lane_change_direction = LaneChangeDirection.left
    elif sm['carState'].rightBlinker:
      self.lane_change_direction = LaneChangeDirection.right

    if (not active) or (self.lane_change_timer > LANE_CHANGE_TIME_MAX) or (not one_blinker) or (not self.lane_change_enabled):
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
    else:
      torque_applied = sm['carState'].steeringPressed and \
                       ((sm['carState'].steeringTorque > 0 and self.lane_change_direction == LaneChangeDirection.left) or \
                        (sm['carState'].steeringTorque < 0 and self.lane_change_direction == LaneChangeDirection.right))

      lane_change_prob = self.LP.l_lane_change_prob + self.LP.r_lane_change_prob

      # dragonpilot auto lc
      if not below_lane_change_speed and self.dragon_auto_lc_enabled and v_ego >= self.dragon_auto_lc_min_mph:
        # we allow auto lc when speed reached dragon_auto_lc_min_mph
        self.dragon_auto_lc_allowed = True
      else:
        # if too slow, we reset all the variables
        self.dragon_auto_lc_allowed = False
        self.dragon_auto_lc_timer = None

      # disable auto lc when continuous is off and already did auto lc once
      if self.dragon_auto_lc_allowed and not self.dp_continuous_auto_lc and self.dp_did_auto_lc:
        self.dragon_auto_lc_allowed = False

      if self.dragon_auto_lc_allowed:
        if self.dragon_auto_lc_timer is None:
          # we only set timer when in preLaneChange state, dragon_auto_lc_delay delay
          if self.lane_change_state == LaneChangeState.preLaneChange:
            self.dragon_auto_lc_timer = cur_time + self.dragon_auto_lc_delay
        elif cur_time >= self.dragon_auto_lc_timer:
          # if timer is up, we set torque_applied to True to fake user input
          torque_applied = True
          self.dp_did_auto_lc = True

      # we reset the timers when torque is applied regardless
      if torque_applied:
        self.dragon_auto_lc_timer = None

      # State transitions
      # off
      if self.lane_change_state == LaneChangeState.off and one_blinker and not self.prev_one_blinker and not below_lane_change_speed:
        self.lane_change_state = LaneChangeState.preLaneChange
        self.lane_change_ll_prob = 1.0

      # pre
      elif self.lane_change_state == LaneChangeState.preLaneChange:
        if not one_blinker or below_lane_change_speed:
          self.lane_change_state = LaneChangeState.off
        elif torque_applied:
          self.lane_change_state = LaneChangeState.laneChangeStarting

      # starting
      elif self.lane_change_state == LaneChangeState.laneChangeStarting:
        # fade out over .2s
        self.lane_change_ll_prob = max(self.lane_change_ll_prob - DT_MDL/5, 0.0)
        # 98% certainty
        if lane_change_prob < 0.02 and self.lane_change_ll_prob < 0.01:
          self.lane_change_state = LaneChangeState.laneChangeFinishing

      # finishing
      elif self.lane_change_state == LaneChangeState.laneChangeFinishing:
        # fade in laneline over 1s
        self.lane_change_ll_prob = min(self.lane_change_ll_prob + DT_MDL, 1.0)
        if one_blinker and self.lane_change_ll_prob > 0.99:
          self.lane_change_state = LaneChangeState.preLaneChange
        elif self.lane_change_ll_prob > 0.99:
          self.lane_change_state = LaneChangeState.off

        # when finishing, we reset timer to none.
        self.dragon_auto_lc_timer = None

    if self.lane_change_state in [LaneChangeState.off, LaneChangeState.preLaneChange]:
      self.lane_change_timer = 0.0
    else:
      self.lane_change_timer += DT_MDL

    if self.prev_one_blinker and not one_blinker:
      self.dp_did_auto_lc = False

    self.prev_one_blinker = one_blinker

    desire = DESIRES[self.lane_change_direction][self.lane_change_state]

    # Turn off lanes during lane change
    if desire == log.PathPlan.Desire.laneChangeRight or desire == log.PathPlan.Desire.laneChangeLeft:
      self.LP.l_prob *= self.lane_change_ll_prob
      self.LP.r_prob *= self.lane_change_ll_prob
    self.LP.update_d_poly(v_ego)

    # account for actuation delay
    self.cur_state = calc_states_after_delay(self.cur_state, v_ego, angle_steers - angle_offset, curvature_factor, self.dp_steer_ratio, CP.steerActuatorDelay)

    v_ego_mpc = max(v_ego, 5.0)  # avoid mpc roughness due to low speed
    self.libmpc.run_mpc(self.cur_state, self.mpc_solution,
                        list(self.LP.l_poly), list(self.LP.r_poly), list(self.LP.d_poly),
                        self.LP.l_prob, self.LP.r_prob, curvature_factor, v_ego_mpc, self.LP.lane_width)

    # reset to current steer angle if not active or overriding
    if active:
      delta_desired = self.mpc_solution[0].delta[1]
      rate_desired = math.degrees(self.mpc_solution[0].rate[0] * self.dp_steer_ratio)
    else:
      delta_desired = math.radians(angle_steers - angle_offset) / self.dp_steer_ratio
      rate_desired = 0.0

    self.cur_state[0].delta = delta_desired

    self.angle_steers_des_mpc = float(math.degrees(delta_desired * self.dp_steer_ratio) + angle_offset)

    #  Check for infeasable MPC solution
    mpc_nans = any(math.isnan(x) for x in self.mpc_solution[0].delta)
    t = sec_since_boot()
    if mpc_nans:
      self.libmpc.init(MPC_COST_LAT.PATH, MPC_COST_LAT.LANE, MPC_COST_LAT.HEADING, CP.steerRateCost)
      self.cur_state[0].delta = math.radians(angle_steers - angle_offset) / self.dp_steer_ratio

      if t > self.last_cloudlog_t + 5.0:
        self.last_cloudlog_t = t
        cloudlog.warning("Lateral mpc - nan: True")

    if self.mpc_solution[0].cost > 20000. or mpc_nans:   # TODO: find a better way to detect when MPC did not converge
      self.solution_invalid_cnt += 1
    else:
      self.solution_invalid_cnt = 0
    plan_solution_valid = self.solution_invalid_cnt < 2

    plan_send = messaging.new_message('pathPlan')
    plan_send.valid = sm.all_alive_and_valid(service_list=['carState', 'controlsState', 'liveParameters', 'model'])
    plan_send.pathPlan.laneWidth = float(self.LP.lane_width)
    plan_send.pathPlan.dPoly = [float(x) for x in self.LP.d_poly]
    plan_send.pathPlan.lPoly = [float(x) for x in self.LP.l_poly]
    plan_send.pathPlan.lProb = float(self.LP.l_prob)
    plan_send.pathPlan.rPoly = [float(x) for x in self.LP.r_poly]
    plan_send.pathPlan.rProb = float(self.LP.r_prob)

    plan_send.pathPlan.angleSteers = float(self.angle_steers_des_mpc)
    plan_send.pathPlan.rateSteers = float(rate_desired)
    plan_send.pathPlan.angleOffset = float(sm['liveParameters'].angleOffsetAverage)
    plan_send.pathPlan.mpcSolutionValid = bool(plan_solution_valid)
    plan_send.pathPlan.paramsValid = bool(sm['liveParameters'].valid)
    plan_send.pathPlan.sensorValid = bool(sm['liveParameters'].sensorValid)
    plan_send.pathPlan.posenetValid = bool(sm['liveParameters'].posenetValid)

    plan_send.pathPlan.desire = desire
    plan_send.pathPlan.laneChangeState = self.lane_change_state
    plan_send.pathPlan.laneChangeDirection = self.lane_change_direction
    plan_send.pathPlan.alcAllowed = self.dragon_auto_lc_allowed

    pm.send('pathPlan', plan_send)

    if LOG_MPC:
      dat = messaging.new_message('liveMpc')
      dat.liveMpc.x = list(self.mpc_solution[0].x)
      dat.liveMpc.y = list(self.mpc_solution[0].y)
      dat.liveMpc.psi = list(self.mpc_solution[0].psi)
      dat.liveMpc.delta = list(self.mpc_solution[0].delta)
      dat.liveMpc.cost = self.mpc_solution[0].cost
      pm.send('liveMpc', dat)
