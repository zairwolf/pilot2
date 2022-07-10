import os
import time
from cereal import car
from common.kalman.simple_kalman import KF1D
from common.realtime import DT_CTRL
from selfdrive.car import gen_empty_fingerprint
from selfdrive.controls.lib.drive_helpers import EventTypes as ET, create_event
from selfdrive.controls.lib.vehicle_model import VehicleModel

# dp
from common.realtime import sec_since_boot
from common.params import Params, put_nonblocking
params = Params()
from common.dp import get_last_modified

GearShifter = car.CarState.GearShifter

# generic car and radar interfaces

class CarInterfaceBase():
  def __init__(self, CP, CarController, CarState):
    self.CP = CP
    self.VM = VehicleModel(CP)

    self.frame = 0
    self.low_speed_alert = False

    self.CS = CarState(CP)
    self.cp = self.CS.get_can_parser(CP)
    self.cp_cam = self.CS.get_cam_can_parser(CP)

    self.CC = None
    if CarController is not None:
      self.CC = CarController(self.cp.dbc_name, CP, self.VM)

    # dp
    self.dragon_toyota_stock_dsu = False
    self.dragon_enable_steering_on_signal = False
    self.dragon_allow_gas = False
    self.ts_last_check = 0.
    self.dragon_lat_ctrl = True
    self.dp_last_modified = None
    self.dp_gear_check = True

  @staticmethod
  def calc_accel_override(a_ego, a_target, v_ego, v_target):
    return 1.

  @staticmethod
  def compute_gb(accel, speed):
    raise NotImplementedError

  @staticmethod
  def get_params(candidate, fingerprint=gen_empty_fingerprint(), has_relay=False, car_fw=[]):
    raise NotImplementedError

  # returns a set of default params to avoid repetition in car specific params
  @staticmethod
  def get_std_params(candidate, fingerprint, has_relay):
    ret = car.CarParams.new_message()
    ret.carFingerprint = candidate
    ret.isPandaBlack = has_relay

    # standard ALC params
    ret.steerControlType = car.CarParams.SteerControlType.torque
    ret.steerMaxBP = [0.]
    ret.steerMaxV = [1.]
    ret.minSteerSpeed = 0.

    # stock ACC by default
    ret.enableCruise = True
    ret.minEnableSpeed = -1.  # enable is done by stock ACC, so ignore this
    ret.steerRatioRear = 0.  # no rear steering, at least on the listed cars aboveA
    ret.gasMaxBP = [0.]
    ret.gasMaxV = [.5]  # half max brake
    ret.brakeMaxBP = [0.]
    ret.brakeMaxV = [1.]
    ret.openpilotLongitudinalControl = False
    ret.startAccel = 0.0
    ret.stoppingControl = False
    ret.longitudinalTuning.deadzoneBP = [0.]
    ret.longitudinalTuning.deadzoneV = [0.]
    ret.longitudinalTuning.kpBP = [0.]
    ret.longitudinalTuning.kpV = [1.]
    ret.longitudinalTuning.kiBP = [0.]
    ret.longitudinalTuning.kiV = [1.]
    return ret

  # returns a car.CarState, pass in car.CarControl
  def update(self, c, can_strings):
    raise NotImplementedError

  # return sendcan, pass in a car.CarControl
  def apply(self, c):
    raise NotImplementedError

  def dp_load_params(self, car_name):
    # dp
    ts = sec_since_boot()
    if ts - self.ts_last_check >= 5.:
      modified = get_last_modified()
      if self.dp_last_modified != modified:
        self.dragon_lat_ctrl = False if params.get("DragonLatCtrl", encoding='utf8') == "0" else True
        if self.dragon_lat_ctrl:
          self.dragon_enable_steering_on_signal = True if (params.get("DragonEnableSteeringOnSignal", encoding='utf8') == "1" and params.get("LaneChangeEnabled", encoding='utf8') == "0") else False
        self.dragon_toyota_stock_dsu = True if (car_name == 'toyota' and params.get("DragonToyotaStockDSU", encoding='utf8') == "1") else False
        if not self.dragon_toyota_stock_dsu:
          self.dragon_allow_gas = True if params.get("DragonAllowGas", encoding='utf8') == "1" else False
        self.dp_gear_check = False if params.get("DragonEnableGearCheck", encoding='utf8') == "0" else True
        self.dp_last_modified = modified
      self.ts_last_check = ts

  def create_common_events(self, cs_out, extra_gears=[], gas_resume_speed=-1, pcm_enable=True):
    events = []

    if cs_out.doorOpen:
      events.append(create_event('doorOpen', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if cs_out.seatbeltUnlatched:
      events.append(create_event('seatbeltNotLatched', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if not self.dragon_toyota_stock_dsu:
      if cs_out.gearShifter != GearShifter.drive and cs_out.gearShifter not in extra_gears:
        events.append(create_event('wrongGear', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
      if cs_out.gearShifter == GearShifter.reverse:
        events.append(create_event('reverseGear', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE]))
    if not cs_out.cruiseState.available:
      events.append(create_event('wrongCarMode', [ET.NO_ENTRY, ET.USER_DISABLE]))
    if cs_out.espDisabled:
      events.append(create_event('espDisabled', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if cs_out.gasPressed and not self.dragon_allow_gas and not self.dragon_toyota_stock_dsu:
      events.append(create_event('pedalPressed', [ET.PRE_ENABLE]))

    if not self.dragon_lat_ctrl:
      events.append(create_event('manualSteeringRequired', [ET.WARNING]))
    elif self.dragon_enable_steering_on_signal and (cs_out.leftBlinker or cs_out.rightBlinker):
      events.append(create_event('manualSteeringRequiredBlinkersOn', [ET.WARNING]))
    elif cs_out.steerError:
      events.append(create_event('steerUnavailable', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE, ET.PERMANENT]))
    elif cs_out.steerWarning:
      events.append(create_event('steerTempUnavailable', [ET.NO_ENTRY, ET.WARNING]))

    # Disable on rising edge of gas or brake. Also disable on brake when speed > 0.
    # Optionally allow to press gas at zero speed to resume.
    # e.g. Chrysler does not spam the resume button yet, so resuming with gas is handy. FIXME!
    if not self.dragon_toyota_stock_dsu:
      if not self.dragon_allow_gas:
        if (cs_out.gasPressed and (not self.CS.out.gasPressed) and cs_out.vEgo > gas_resume_speed) or \
            (cs_out.brakePressed and (not self.CS.out.brakePressed or not cs_out.standstill)):
          events.append(create_event('pedalPressed', [ET.NO_ENTRY, ET.USER_DISABLE]))
      else:
        if cs_out.brakePressed and (not self.CS.out.brakePressed or not cs_out.standstill):
          events.append(create_event('pedalPressed', [ET.NO_ENTRY, ET.USER_DISABLE]))

    # we engage when pcm is active (rising edge)
    if pcm_enable:
      if cs_out.cruiseState.enabled and not self.CS.out.cruiseState.enabled:
        events.append(create_event('pcmEnable', [ET.ENABLE]))
      elif not cs_out.cruiseState.enabled:
        events.append(create_event('pcmDisable', [ET.USER_DISABLE]))

    return events

class RadarInterfaceBase():
  def __init__(self, CP):
    self.pts = {}
    self.delay = 0
    self.radar_ts = CP.radarTimeStep

  def update(self, can_strings):
    ret = car.RadarData.new_message()

    if 'NO_RADAR_SLEEP' not in os.environ:
      time.sleep(self.radar_ts)  # radard runs on RI updates

    return ret

class CarStateBase:
  def __init__(self, CP):
    self.CP = CP
    self.car_fingerprint = CP.carFingerprint
    self.cruise_buttons = 0
    self.out = car.CarState.new_message()

    # Q = np.matrix([[10.0, 0.0], [0.0, 100.0]])
    # R = 1e3
    self.v_ego_kf = KF1D(x0=[[0.0], [0.0]],
                         A=[[1.0, DT_CTRL], [0.0, 1.0]],
                         C=[1.0, 0.0],
                         K=[[0.12287673], [0.29666309]])

  def update_speed_kf(self, v_ego_raw):
    if abs(v_ego_raw - self.v_ego_kf.x[0][0]) > 2.0:  # Prevent large accelerations when car starts at non zero speed
      self.v_ego_kf.x = [[v_ego_raw], [0.0]]

    v_ego_x = self.v_ego_kf.update(v_ego_raw)
    return float(v_ego_x[0]), float(v_ego_x[1])

  @staticmethod
  def parse_gear_shifter(gear):
    return {'P': GearShifter.park, 'R': GearShifter.reverse, 'N': GearShifter.neutral,
            'E': GearShifter.eco, 'T': GearShifter.manumatic, 'D': GearShifter.drive,
            'S': GearShifter.sport, 'L': GearShifter.low, 'B': GearShifter.brake}.get(gear, GearShifter.unknown)

  @staticmethod
  def get_cam_can_parser(CP):
    return None
