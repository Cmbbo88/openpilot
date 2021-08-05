from cereal import car
from common.numpy_fast import clip
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.volkswagen import volkswagencan
from selfdrive.car.volkswagen.values import DBC, CANBUS, NWL, MQB_LDW_MESSAGES, BUTTON_STATES, CarControllerParams as P, PQ_LDW_MESSAGES
from opendbc.can.packer import CANPacker

VisualAlert = car.CarControl.HUDControl.VisualAlert

class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.apply_steer_last = 0
    self.mobPreEnable = False
    self.mobEnabled = False
    self.radarVin_idx = 0

    self.packer_pt = CANPacker(DBC[CP.carFingerprint]['pt'])
    self.acc_bus = CANBUS.pt if CP.networkLocation == NWL.fwdCamera else CANBUS.cam

    if CP.safetyModel == car.CarParams.SafetyModel.volkswagen:
      self.create_steering_control = volkswagencan.create_mqb_steering_control
      self.create_acc_buttons_control = volkswagencan.create_mqb_acc_buttons_control
      self.create_hud_control = volkswagencan.create_mqb_hud_control
      self.ldw_step = P.MQB_LDW_STEP
    elif CP.safetyModel == car.CarParams.SafetyModel.volkswagenPq:
      self.create_steering_control = volkswagencan.create_pq_steering_control
      self.create_acc_buttons_control = volkswagencan.create_pq_acc_buttons_control
      self.create_hud_control = volkswagencan.create_pq_hud_control
      self.create_braking_control = volkswagencan.create_pq_braking_control
      self.create_gas_control = volkswagencan.create_pq_pedal_control
      self.create_awv_control = volkswagencan.create_pq_awv_control
      self.ldw_step = P.PQ_LDW_STEP

    self.hcaSameTorqueCount = 0
    self.hcaEnabledFrameCount = 0
    self.graButtonStatesToSend = None
    self.graMsgSentCount = 0
    self.graMsgStartFramePrev = 0
    self.graMsgBusCounterPrev = 0

    self.steer_rate_limited = False

  def update(self, enabled, CS, frame, actuators, visual_alert, left_lane_visible, right_lane_visible, left_lane_depart, right_lane_depart):
    """ Controls thread """

    can_sends = []

    # **** Steering Controls ************************************************ #

    if frame % P.HCA_STEP == 0:
      # Logic to avoid HCA state 4 "refused":
      #   * Don't steer unless HCA is in state 3 "ready" or 5 "active"
      #   * Don't steer at standstill
      #   * Don't send > 3.00 Newton-meters torque
      #   * Don't send the same torque for > 6 seconds
      #   * Don't send uninterrupted steering for > 360 seconds
      # One frame of HCA disabled is enough to reset the timer, without zeroing the
      # torque value. Do that anytime we happen to have 0 torque, or failing that,
      # when exceeding ~1/3 the 360 second timer.

      if enabled and not (CS.out.standstill or CS.out.steerError or CS.out.steerWarning):
        new_steer = int(round(actuators.steer * P.STEER_MAX))
        apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, P)
        self.steer_rate_limited = new_steer != apply_steer

        #STUFF FOR PQTIMEBOMB BYPASS
        if CS.out.stopSteering:
          apply_steer = 0

        # FAULT AVOIDANCE: HCA must not be enabled for >360 seconds. Sending
        # a single frame with HCA disabled is an effective workaround.
        if apply_steer == 0:
          hcaEnabled = False
          self.hcaEnabledFrameCount = 0
        else:
          self.hcaEnabledFrameCount += 1
          if self.hcaEnabledFrameCount >= 118 * (100 / P.HCA_STEP):  # 118s
            hcaEnabled = False
            self.hcaEnabledFrameCount = 0
          else:
            hcaEnabled = True
            if self.apply_steer_last == apply_steer:
              self.hcaSameTorqueCount += 1
              if self.hcaSameTorqueCount > 1.9 * (100 / P.HCA_STEP):  # 1.9s
                apply_steer -= (1, -1)[apply_steer < 0]
                self.hcaSameTorqueCount = 0
            else:
              self.hcaSameTorqueCount = 0
      else:
        hcaEnabled = False
        apply_steer = 0

      self.apply_steer_last = apply_steer
      idx = (frame / P.HCA_STEP) % 16
      can_sends.append(self.create_steering_control(self.packer_pt, CANBUS.pt, apply_steer,
                                                                 idx, hcaEnabled))

    # --------------------------------------------------------------------------
    #                                                                         #
    # Prepare PQ_MOB for sending the braking message                          #
    #                                                                         #
    #                                                                         #
    # --------------------------------------------------------------------------
    if (frame % P.MOB_STEP == 0) and CS.CP.enableGasInterceptor:
      mobEnabled = self.mobEnabled
      mobPreEnable = self.mobPreEnable
      # TODO make sure we use the full 8190 when calculating braking.
      apply_brake = actuators.brake * 650
      stopping_wish = False

      if enabled:
        if (apply_brake < 5):
          apply_brake = 0
        if apply_brake > 0:
          if not mobEnabled:
            mobEnabled = True
            apply_brake = 0
          elif not mobPreEnable:
            mobPreEnable = True
            apply_brake = 0
          elif apply_brake > 1199:
            apply_brake = 1200
            CS.brake_warning = True
          if CS.currentSpeed < 5.6:
            stopping_wish = True
        else:
          mobPreEnable = False
          mobEnabled = False
      else:
        apply_brake = 0
        mobPreEnable = False
        mobEnabled = False

      idx = (frame / P.MOB_STEP) % 16
      self.mobPreEnable = mobPreEnable
      self.mobEnabled = mobEnabled
      can_sends.append(self.create_braking_control(self.packer_pt, CANBUS.pt, apply_brake, idx, mobEnabled, mobPreEnable, stopping_wish))

      # --------------------------------------------------------------------------
      #                                                                         #
      # Prepare PQ_AWV for Front Assist LED and Front Assist Text               #
      #                                                                         #
      #                                                                         #
      # --------------------------------------------------------------------------
      if (frame % P.AWV_STEP == 0) and CS.CP.enableGasInterceptor:
        green_led = 1 if enabled else 0
        orange_led = 1 if self.mobPreEnable and self.mobEnabled else 0
        if enabled:
          braking_working = 0 if (CS.ABSWorking == 0) else 5
        else:
          braking_working = 0

        idx = (frame / P.MOB_STEP) % 16

        can_sends.append(
          self.create_awv_control(self.packer_pt, CANBUS.pt, idx, orange_led, green_led, braking_working))

    # --------------------------------------------------------------------------
    #                                                                         #
    # Prepare GAS_COMMAND for sending towards Pedal                           #
    #                                                                         #
    #                                                                         #
    # --------------------------------------------------------------------------
    if (frame % P.GAS_STEP == 0) and CS.CP.enableGasInterceptor:
      apply_gas = 0
      if enabled:
        apply_gas = clip(actuators.gas, 0., 1.)

      can_sends.append(self.create_gas_control(self.packer_pt, CANBUS.pt, apply_gas, frame // 2))

    # --------------------------------------------------------------------------
    #                                                                         #
    # Prepare VIN_MESSAGE for sending towards Panda                           #
    #                                                                         #
    #                                                                         #
    # --------------------------------------------------------------------------
    # if using radar, we need to send the VIN
    #if CS.useTeslaRadar and (frame % 100 == 0):
    #  can_sends.append(
    #    volkswagencan.create_radar_VIN_msg(self.radarVin_idx, CS.radarVIN, 2, 0x4A0, CS.useTeslaRadar,
    #                                        CS.radarPosition,
    #                                        CS.radarEpasType))
    #  self.radarVin_idx += 1
    #  self.radarVin_idx = self.radarVin_idx % 3

    #--------------------------------------------------------------------------
    #                                                                         #
    # Prepare LDW_02 HUD messages with lane borders, confidence levels, and   #
    # the LKAS status LED.                                                    #
    #                                                                         #
    #--------------------------------------------------------------------------

    # The factory camera emits this message at 10Hz. When OP is active, Panda
    # filters LDW_02 from the factory camera and OP emits LDW_02 at 10Hz.

    if frame % self.ldw_step == 0:
      hcaEnabled = True if enabled and not CS.out.standstill else False

      if visual_alert == car.CarControl.HUDControl.VisualAlert.steerRequired:
        hud_alert = PQ_LDW_MESSAGES["laneAssistTakeOverSilent"]
      else:
        hud_alert = PQ_LDW_MESSAGES["none"]

      can_sends.append(self.create_hud_control(self.packer_pt, CANBUS.pt, hcaEnabled,
                                                            CS.out.steeringPressed, hud_alert, left_lane_visible,
                                                            right_lane_visible, CS.ldw_lane_warning_left,
                                                            CS.ldw_lane_warning_right, CS.ldw_side_dlc_tlc,
                                                            CS.ldw_dlc, CS.ldw_tlc))

    # **** ACC Button Controls ********************************************** #

    # FIXME: this entire section is in desperate need of refactoring

    if frame > self.graMsgStartFramePrev + P.GRA_VBP_STEP:
      if not enabled and CS.out.cruiseState.enabled:
        # Cancel ACC if it's engaged with OP disengaged.
        self.graButtonStatesToSend = BUTTON_STATES.copy()
        self.graButtonStatesToSend["cancel"] = True
      elif enabled and CS.out.standstill:
        # Blip the Resume button if we're engaged at standstill.
        # FIXME: This is a naive implementation, improve with visiond or radar input.
        # A subset of MQBs like to "creep" too aggressively with this implementation.
        self.graButtonStatesToSend = BUTTON_STATES.copy()
        self.graButtonStatesToSend["resumeCruise"] = True
      elif enabled and CS.out.cruiseState.enabled and CS.CP.enableGasInterceptor:
        self.graButtonStatesToSend = BUTTON_STATES.copy()
        self.graButtonStatesToSend["cancel"] = True


    if CS.graMsgBusCounter != self.graMsgBusCounterPrev:
      self.graMsgBusCounterPrev = CS.graMsgBusCounter
      if self.graButtonStatesToSend is not None:
        if self.graMsgSentCount == 0:
          self.graMsgStartFramePrev = frame
        idx = (CS.graMsgBusCounter + 1) % 16
        can_sends.append(self.create_acc_buttons_control(self.packer_pt, CANBUS.pt, self.graButtonStatesToSend, CS, idx))
        self.graMsgSentCount += 1
        if self.graMsgSentCount >= P.GRA_VBP_COUNT:
          self.graButtonStatesToSend = None
          self.graMsgSentCount = 0

    return can_sends
