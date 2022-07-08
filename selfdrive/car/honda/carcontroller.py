from collections import namedtuple

from cereal import car
from common.conversions import Conversions as CV
from common.numpy_fast import clip, interp
from common.realtime import DT_CTRL
from opendbc.can.packer import CANPacker
from selfdrive.car import create_gas_interceptor_command
from selfdrive.car.honda import hondacan
from selfdrive.car.honda.values import CruiseButtons, VISUAL_HUD, HONDA_BOSCH, HONDA_NIDEC_ALT_PCM_ACCEL, CarControllerParams
from selfdrive.controls.lib.drive_helpers import rate_limit
from common.params import Params
import cereal.messaging as messaging

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState


def compute_gb_honda_bosch(accel, speed):
  # TODO returns 0s, is unused
  return 0.0, 0.0


def compute_gb_honda_nidec(accel, speed):
  creep_brake = 0.0
  creep_speed = 2.3
  creep_brake_value = 0.15
  if speed < creep_speed:
    creep_brake = (creep_speed - speed) / creep_speed * creep_brake_value
  gb = float(accel) / 4.8 - creep_brake
  return clip(gb, 0.0, 1.0), clip(-gb, 0.0, 1.0)


def compute_gas_brake(accel, speed, fingerprint):
  if fingerprint in HONDA_BOSCH:
    return compute_gb_honda_bosch(accel, speed)
  else:
    return compute_gb_honda_nidec(accel, speed)


# TODO not clear this does anything useful
def actuator_hysteresis(brake, braking, brake_steady, v_ego, car_fingerprint):
  # hyst params
  brake_hyst_on = 0.02    # to activate brakes exceed this value
  brake_hyst_off = 0.005  # to deactivate brakes below this value
  brake_hyst_gap = 0.01   # don't change brake command for small oscillations within this value

  # *** hysteresis logic to avoid brake blinking. go above 0.1 to trigger
  if (brake < brake_hyst_on and not braking) or brake < brake_hyst_off:
    brake = 0.
  braking = brake > 0.

  # for small brake oscillations within brake_hyst_gap, don't change the brake command
  if brake == 0.:
    brake_steady = 0.
  elif brake > brake_steady + brake_hyst_gap:
    brake_steady = brake - brake_hyst_gap
  elif brake < brake_steady - brake_hyst_gap:
    brake_steady = brake + brake_hyst_gap
  brake = brake_steady

  return brake, braking, brake_steady


def brake_pump_hysteresis(apply_brake, apply_brake_last, last_pump_ts, ts):
  pump_on = False

  # reset pump timer if:
  # - there is an increment in brake request
  # - we are applying steady state brakes and we haven't been running the pump
  #   for more than 20s (to prevent pressure bleeding)
  if apply_brake > apply_brake_last or (ts - last_pump_ts > 20. and apply_brake > 0):
    last_pump_ts = ts

  # once the pump is on, run it for at least 0.2s
  if ts - last_pump_ts < 0.2 and apply_brake > 0:
    pump_on = True

  return pump_on, last_pump_ts


def process_hud_alert(hud_alert):
  # initialize to no alert
  fcw_display = 0
  steer_required = 0
  acc_alert = 0

  # priority is: FCW, steer required, all others
  if hud_alert == VisualAlert.fcw:
    fcw_display = VISUAL_HUD[hud_alert.raw]
  elif hud_alert in (VisualAlert.steerRequired, VisualAlert.ldw):
    steer_required = VISUAL_HUD[hud_alert.raw]
  else:
    acc_alert = VISUAL_HUD[hud_alert.raw]

  return fcw_display, steer_required, acc_alert


HUDData = namedtuple("HUDData",
                     ["pcm_accel", "v_cruise", "car",
                      "lanes", "fcw", "acc_alert", "steer_required", "dashed_lanes"])


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.packer = CANPacker(dbc_name)
    self.params = CarControllerParams(CP)
    self.frame = 0

    self.braking = False
    self.brake_steady = 0.
    self.brake_last = 0.
    self.apply_brake_last = 0
    self.last_pump_ts = 0.

    self.accel = 0.0
    self.speed = 0.0
    self.gas = 0.0
    self.brake = 0.0

    self.signal_last = 0.
    self.lat_active = False
    self.init_main_off = False
    self.standstill_status = 0
    self.standstill_status_timer = 0

    self.sm = messaging.SubMaster(['liveMapData', 'controlsState', 'longitudinalPlan'])
    self.param_s = Params()
    self.speed_limit_control_enabled = self.param_s.get_bool("SpeedLimitControl")
    self.vision_turn_speed_control = self.param_s.get_bool("VisionTurnSpeedControl")
    self.map_turn_speed_control = self.param_s.get_bool("MapTurnSpeedControl")
    self.last_speed_limit_sign_tap = self.param_s.get_bool("LastSpeedLimitSignTap")
    self.speed_limit = 0.
    self.is_metric = self.param_s.get_bool("IsMetric")
    self.speed_limit_perc_offset = self.param_s.get_bool("SpeedLimitPercOffset")
    self.speed_limit_value_offset = int(self.param_s.get("SpeedLimitValueOffset"))
    self.v_cruise_kph_prev = self.sm['controlsState'].vCruise
    self.timer = 0
    self.final_speed_kph = 0
    self.init_speed = 0
    self.current_speed = 0
    self.v_set_dis = 0
    self.button_type = 0
    self.button_select = 0
    self.button_count = 0
    self.target_speed = 0
    self.resume_count = 0
    self.t_interval = 7
    self.sl_force_active_timer = 0
    self.slc_state = 0
    self.beep_timer = 0.
    self.speed_limit_changed_timer = 0
    self.speed_limit_prev = 0.
    self.slc_active_stock = False

  def update(self, CC, CS):
    self.sm.update(0)
    self.speed_limit_control_enabled = self.param_s.get_bool("SpeedLimitControl")
    self.vision_turn_speed_control = self.param_s.get_bool("VisionTurnSpeedControl")
    self.map_turn_speed_control = self.param_s.get_bool("MapTurnSpeedControl")
    self.last_speed_limit_sign_tap = self.param_s.get_bool("LastSpeedLimitSignTap")
    self.slc_state = self.sm['longitudinalPlan'].speedLimitControlState
    self.is_metric = self.param_s.get_bool("IsMetric")
    self.speed_limit_perc_offset = self.param_s.get_bool("SpeedLimitPercOffset")
    self.speed_limit_value_offset = int(self.param_s.get("SpeedLimitValueOffset"))
    self.v_cruise_kph_prev = self.sm['controlsState'].vCruise
    self.get_speed_limit()

    actuators = CC.actuators
    hud_control = CC.hudControl
    hud_v_cruise = hud_control.setSpeed * CV.MS_TO_KPH if hud_control.speedVisible else 255
    pcm_cancel_cmd = CC.cruiseControl.cancel

    if CC.longActive:
      accel = actuators.accel
      gas, brake = compute_gas_brake(actuators.accel, CS.out.vEgo, self.CP.carFingerprint)
    else:
      accel = 0.0
      gas, brake = 0.0, 0.0

    # *** apply brake hysteresis ***
    pre_limit_brake, self.braking, self.brake_steady = actuator_hysteresis(brake, self.braking, self.brake_steady,
                                                                           CS.out.vEgo, self.CP.carFingerprint)

    # *** rate limit after the enable check ***
    self.brake_last = rate_limit(pre_limit_brake, self.brake_last, -2., DT_CTRL)

    cur_time = self.frame * DT_CTRL
    if CS.leftBlinkerOn or CS.rightBlinkerOn:
      self.signal_last = cur_time

    # vehicle hud display, wait for one update from 10Hz 0x304 msg
    if hud_control.lanesVisible and CC.latActive:
      hud_lanes = 1
    else:
      hud_lanes = 0

    if CC.enabled and CS.out.cruiseState.enabled:
      if hud_control.leadVisible:
        hud_car = 2
      else:
        hud_car = 1
    else:
      hud_car = 0

    fcw_display, steer_required, acc_alert = process_hud_alert(hud_control.visualAlert)

    # **** process the car messages ****

    # steer torque is converted back to CAN reference (positive when steering right)
    apply_steer = int(interp(-actuators.steer * self.params.STEER_MAX,
                             self.params.STEER_LOOKUP_BP, self.params.STEER_LOOKUP_V))

    # Send CAN commands
    can_sends = []

    # tester present - w/ no response (keeps radar disabled)
    if self.CP.carFingerprint in HONDA_BOSCH and self.CP.openpilotLongitudinalControl:
      if self.frame % 10 == 0:
        can_sends.append((0x18DAB0F1, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", 1))

    # Send steering command.
    idx = self.frame % 4
    can_sends.append(hondacan.create_steering_control(self.packer, apply_steer, CC.latActive, self.CP.carFingerprint,
                                                      idx, CS.CP.openpilotLongitudinalControl))

    stopping = actuators.longControlState == LongCtrlState.stopping

    # wind brake from air resistance decel at high speed
    wind_brake = interp(CS.out.vEgo, [0.0, 2.3, 35.0], [0.001, 0.002, 0.15])
    # all of this is only relevant for HONDA NIDEC
    max_accel = interp(CS.out.vEgo, self.params.NIDEC_MAX_ACCEL_BP, self.params.NIDEC_MAX_ACCEL_V)
    # TODO this 1.44 is just to maintain previous behavior
    pcm_speed_BP = [-wind_brake,
                    -wind_brake * (3 / 4),
                    0.0,
                    0.5]
    # The Honda ODYSSEY seems to have different PCM_ACCEL
    # msgs, is it other cars too?
    if self.CP.enableGasInterceptor or not CC.longActive:
      pcm_speed = 0.0
      pcm_accel = int(0.0)
    elif self.CP.carFingerprint in HONDA_NIDEC_ALT_PCM_ACCEL:
      pcm_speed_V = [0.0,
                     clip(CS.out.vEgo - 3.0, 0.0, 100.0),
                     clip(CS.out.vEgo + 0.0, 0.0, 100.0),
                     clip(CS.out.vEgo + 5.0, 0.0, 100.0)]
      pcm_speed = interp(gas - brake, pcm_speed_BP, pcm_speed_V)
      pcm_accel = int(1.0 * 0xc6)
    else:
      pcm_speed_V = [0.0,
                     clip(CS.out.vEgo - 2.0, 0.0, 100.0),
                     clip(CS.out.vEgo + 2.0, 0.0, 100.0),
                     clip(CS.out.vEgo + 5.0, 0.0, 100.0)]
      pcm_speed = interp(gas - brake, pcm_speed_BP, pcm_speed_V)
      pcm_accel = int(clip((accel / 1.44) / max_accel, 0.0, 1.0) * 0xc6)

    if self.last_speed_limit_sign_tap:
      self.sl_force_active_timer = cur_time

    sl_force_active = self.speed_limit_control_enabled and (cur_time < self.sl_force_active_timer + 2.0)
    sl_inactive = not sl_force_active and (not self.speed_limit_control_enabled or (True if self.slc_state == 0 else False))
    sl_temp_inactive = not sl_force_active and (self.speed_limit_control_enabled and (True if self.slc_state == 1 else False))
    slc_active = not sl_inactive and not sl_temp_inactive

    self.slc_active_stock = slc_active

    if not slc_active:
      self.beep_timer = cur_time

    beep = slc_active and CS.out.cruiseState.enabled and not ((cur_time - self.beep_timer) > 0.1)

    if self.speed_limit_prev == self.speed_limit:
      self.speed_limit_changed_timer = cur_time

    speed_limit_changed = self.speed_limit_prev != self.speed_limit and not ((cur_time - self.speed_limit_changed_timer) > 0.1)
    if not speed_limit_changed:
      self.speed_limit_prev = self.speed_limit

    if not self.CP.openpilotLongitudinalControl:
      if self.frame % 2 == 0:
        idx = self.frame // 2
        can_sends.append(hondacan.create_bosch_supplemental_1(self.packer, self.CP.carFingerprint, idx))
      # If using stock ACC, spam cancel command to kill gas when OP disengages.
      if CS.out.cruiseState.standstill:
        self.standstill_status = 1
      if pcm_cancel_cmd:
        can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.CANCEL, idx, self.CP.carFingerprint))
      elif CC.cruiseControl.resume:
        can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.RES_ACCEL, idx, self.CP.carFingerprint))
      if not CS.out.cruiseState.standstill and CS.out.cruiseState.enabled and not CS.out.gasPressed:
        cruise_button = self.get_cruise_buttons(CS, self.v_cruise_kph_prev)
        if cruise_button is not None:
          can_sends.append(hondacan.spam_buttons_command(self.packer, cruise_button, self.resume_count // 2, CS.CP.carFingerprint))
          self.resume_count += 1
        else:
          self.resume_count = 0

    else:
      # Send gas and brake commands.
      if self.frame % 2 == 0:
        idx = self.frame // 2
        ts = self.frame * DT_CTRL

        if self.CP.carFingerprint in HONDA_BOSCH:
          self.accel = clip(accel, self.params.BOSCH_ACCEL_MIN, self.params.BOSCH_ACCEL_MAX)
          self.gas = interp(accel, self.params.BOSCH_GAS_LOOKUP_BP, self.params.BOSCH_GAS_LOOKUP_V)
          if not CS.out.cruiseState.enabled:
            self.gas = 0.
          can_sends.extend(hondacan.create_acc_commands(self.packer, CC.enabled, CC.longActive, self.accel, self.gas,
                                                        idx, stopping, self.CP.carFingerprint))
        else:
          apply_brake = clip(self.brake_last - wind_brake, 0.0, 1.0)
          apply_brake = int(clip(apply_brake * self.params.NIDEC_BRAKE_MAX, 0, self.params.NIDEC_BRAKE_MAX - 1))
          if not CS.out.cruiseState.enabled and not (CS.CP.pcmCruise and CS.accEnabled and CS.CP.minEnableSpeed > 0):
            apply_brake = 0.
          pump_on, self.last_pump_ts = brake_pump_hysteresis(apply_brake, self.apply_brake_last, self.last_pump_ts, ts)

          pcm_override = True
          can_sends.append(hondacan.create_brake_command(self.packer, apply_brake, pump_on,
                                                         pcm_override, pcm_cancel_cmd, fcw_display, idx,
                                                         self.CP.carFingerprint, CS.stock_brake))
          self.apply_brake_last = apply_brake
          self.brake = apply_brake / self.params.NIDEC_BRAKE_MAX

          if self.CP.enableGasInterceptor:
            # way too aggressive at low speed without this
            gas_mult = interp(CS.out.vEgo, [0., 10.], [0.4, 1.0])
            # send exactly zero if apply_gas is zero. Interceptor will send the max between read value and apply_gas.
            # This prevents unexpected pedal range rescaling
            # Sending non-zero gas when OP is not enabled will cause the PCM not to respond to throttle as expected
            # when you do enable.
            if CC.longActive:
              self.gas = clip(gas_mult * (gas - brake + wind_brake * 3 / 4), 0., 1.)
            else:
              self.gas = 0.0
            can_sends.append(create_gas_interceptor_command(self.packer, self.gas, idx))

    if CS.out.brakeLights and CS.out.vEgo < 0.1:
      self.standstill_status = 1
      self.standstill_status_timer += 1
      if self.standstill_status_timer > 200:
        self.standstill_status = 1
        self.standstill_status_timer = 0
    if self.standstill_status == 1 and CS.out.vEgo > 1:
      self.standstill_status = 0

    # Send dashboard UI commands.
    if self.frame % 10 == 0:
      idx = (self.frame // 10) % 4
      hud = HUDData(int(pcm_accel), int(round(hud_v_cruise)) if hud_car != 0 else 255, hud_car,
                    hud_lanes, fcw_display, acc_alert, steer_required, CS.madsEnabled and not CC.latActive)
      can_sends.extend(hondacan.create_ui_commands(self.packer, self.CP, pcm_speed, hud, CS.is_metric, idx, CS.stock_hud, CS.gap_adjust_cruise_tr, beep, speed_limit_changed, slc_active))

      if self.CP.openpilotLongitudinalControl and self.CP.carFingerprint not in HONDA_BOSCH:
        self.speed = pcm_speed

        if not self.CP.enableGasInterceptor:
          self.gas = pcm_accel / 0xc6

    self.lat_active = CC.latActive

    new_actuators = actuators.copy()
    new_actuators.speed = self.speed
    new_actuators.accel = self.accel
    new_actuators.gas = self.gas
    new_actuators.brake = self.brake

    self.frame += 1
    return new_actuators, can_sends

  def get_speed_limit(self):
    self.speed_limit = float(self.sm['longitudinalPlan'].speedLimit if self.sm['longitudinalPlan'].speedLimit is not None else 0.0)
    return self.speed_limit

  # multikyd methods, sunnyhaibin logic
  def get_cruise_buttons_status(self, CS):
    if not CS.out.cruiseState.enabled or CS.cruise_buttons != 0:
      self.timer = 40
    elif self.timer:
      self.timer -= 1
    else:
      return 1
    return 0

  def get_target_speed(self, v_cruise_kph_prev):
    v_cruise_kph = v_cruise_kph_prev
    if self.speed_limit_control_enabled and self.speed_limit != 0.0:
      target_v_cruise_kph = self.speed_limit * CV.MS_TO_KPH
      if self.speed_limit_perc_offset:
        v_cruise_kph = target_v_cruise_kph + float(float(self.sm['longitudinalPlan'].speedLimitOffset) * CV.MS_TO_KPH)
      else:
        v_cruise_kph = target_v_cruise_kph + (float(self.speed_limit_value_offset * CV.MPH_TO_KPH) if not self.is_metric else self.speed_limit_value_offset)
      if not self.slc_active_stock:
        v_cruise_kph = v_cruise_kph_prev
    return v_cruise_kph

  def get_button_type(self, button_type):
    self.type_status = "type_" + str(button_type)
    self.button_picker = getattr(self, self.type_status, lambda:"default")
    return self.button_picker()

  def reset_button(self):
    if self.button_type != 3:
      self.button_type = 0

  def type_default(self):
    self.button_type = 0
    return None

  def type_0(self):
    self.button_count = 0
    self.target_speed = self.init_speed
    speed_diff = round(self.target_speed - self.v_set_dis)
    if speed_diff > 0:
      self.button_type = 1
    elif speed_diff < 0:
      self.button_type = 2
    return None

  def type_1(self):
    cruise_button = CruiseButtons.RES_ACCEL
    self.button_count += 1
    if self.target_speed == self.v_set_dis:
      self.button_count = 0
      self.button_type = 3
    elif self.button_count > 5:
      self.button_count = 0
      self.button_type = 3
    return cruise_button

  def type_2(self):
    cruise_button = CruiseButtons.DECEL_SET
    self.button_count += 1
    if self.target_speed == self.v_set_dis:
      self.button_count = 0
      self.button_type = 3
    elif self.button_count > 5:
      self.button_count = 0
      self.button_type = 3
    return cruise_button

  def type_3(self):
    cruise_button = None
    self.button_count += 1
    if self.button_count > self.t_interval:
      self.button_type = 0
    return cruise_button

  def get_curve_speed(self, target_speed_kph, v_cruise_kph_prev):
    if self.vision_turn_speed_control:
      vision_v_cruise_kph = float(float(self.sm['longitudinalPlan'].visionTurnSpeed) * CV.MS_TO_KPH)
      if int(vision_v_cruise_kph) == int(v_cruise_kph_prev):
        vision_v_cruise_kph = 160
    else:
      vision_v_cruise_kph = 160
    if self.map_turn_speed_control:
      map_v_cruise_kph = float(float(self.sm['longitudinalPlan'].turnSpeed) * CV.MS_TO_KPH)
      if int(map_v_cruise_kph) == 0.0:
        map_v_cruise_kph = 160
    else:
      map_v_cruise_kph = 160
    return min(target_speed_kph, vision_v_cruise_kph, map_v_cruise_kph)

  def get_button_control(self, CS, final_speed, v_cruise_kph_prev):
    self.init_speed = int(round((float(min(final_speed, v_cruise_kph_prev)) * 0.6233 + 0.0995))) if not self.is_metric else round(min(final_speed, v_cruise_kph_prev))
    self.v_set_dis = int(round((float(CS.out.cruiseState.speed * CV.MS_TO_KPH) * 0.6233 + 0.0995))) if not self.is_metric else round(CS.out.cruiseState.speed * CV.MS_TO_KPH)
    cruise_button = self.get_button_type(self.button_type)
    return cruise_button

  def get_cruise_buttons(self, CS, v_cruise_kph_prev):
    cruise_button = None
    if not self.get_cruise_buttons_status(CS):
      pass
    elif CS.out.cruiseState.enabled:
      set_speed_kph = self.get_target_speed(v_cruise_kph_prev)
      if self.speed_limit_control_enabled:
        target_speed_kph = set_speed_kph
      else:
        target_speed_kph = min(v_cruise_kph_prev, set_speed_kph)
      if self.vision_turn_speed_control or self.map_turn_speed_control:
        self.final_speed_kph = self.get_curve_speed(target_speed_kph, v_cruise_kph_prev)
      else:
        self.final_speed_kph = target_speed_kph
      cruise_button = self.get_button_control(CS, self.final_speed_kph, v_cruise_kph_prev) # MPH/KPH based button presses
    return cruise_button
