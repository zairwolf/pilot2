#!/usr/bin/env python3
'''
This is a service that broadcast dp config values to openpilot's messaging queues
'''
import cereal.messaging as messaging
import time
import datetime
from common.dp_conf import confs, get_struct_name, to_param_val, to_struct_val
from common.params import Params, put_nonblocking
import subprocess
import re
import os
from common.android import getprop
from selfdrive.thermald.power_monitoring import set_battery_charging, get_battery_charging
from math import floor
params = Params()
from common.realtime import sec_since_boot

DASHCAM_VIDEOS_PATH = '/sdcard/dashcam/'
DASHCAM_DURATION = 180 # max is 180
DASHCAM_BIT_RATES = 4000000 # max is 4000000
DASHCAM_MAX_SIZE_PER_FILE = DASHCAM_BIT_RATES/8*DASHCAM_DURATION # 4Mbps / 8 * 180 = 90MB per 180 seconds
DASHCAM_FREESPACE_LIMIT = 0.15 # we start cleaning up footage when freespace is below 15%
DASHCAM_KEPT = DASHCAM_MAX_SIZE_PER_FILE * 240 # 12 hrs of video = 21GB

def confd_thread():
  sm = messaging.SubMaster(['thermal'])
  pm = messaging.PubMaster(['dragonConf'])
  last_dp_msg = None
  last_modified = None
  update_params = False
  frame = 0
  locale = getprop("persist.sys.locale").rstrip('\n')

  last_autoshutdown = False
  last_sec = None
  autoshutdown_frame = 0

  last_charging_ctrl = False
  last_started = False

  dashcam_next_frame = 0
  dashcam_folder_exists = False
  dashcam_mkdir_retry = 0

  # thermal
  started = False
  free_space = 0.
  online = False
  battery_percent = 0.
  overheat = False

  while True:
    start_sec = sec_since_boot()
    if frame % 6 == 0:
      sm.update()
      if sm.updated['thermal']:
        started = sm['thermal'].started
        free_space = sm['thermal'].freeSpace
        online = sm['thermal'].usbOnline
        battery_percent = sm['thermal'].batteryPercent
        overheat = sm['thermal'].thermalStatus >= 2

    msg = messaging.new_message('dragonConf')
    if last_dp_msg is not None:
      msg.dragonConf = last_dp_msg

    '''
    ===================================================
    force update when start status changed
    ===================================================
    '''
    if last_started != started:
      update_params = True
    '''
    ===================================================
    we only need to check last_modified every 3 seconds
    if val is modified, we then proceed to read params
    ===================================================
    '''
    if not update_params and frame % 6 == 0:
      try:
        modified = params.get('dp_last_modified', encoding='utf8').rstrip('\x00')
      except AttributeError:
        modified = str(floor(time.time()))
      if last_modified != modified:
        update_params = True
      last_modified = modified
    '''
    ===================================================
    push param vals to message
    ===================================================
    '''
    if update_params:
      for conf in confs:
        conf_type = conf.get('conf_type')
        if 'param' in conf_type and 'struct' in conf_type:
          update_this_conf = True

          update_once = conf.get('update_once')
          if update_once is not None and update_once is True and frame > 0:
            update_this_conf = False

          if frame > 0 and update_this_conf:
            dependencies = conf.get('depends')
            # if has dependency and the depend param val is not in depend_vals, we dont update that conf val
            # this should reduce chance of reading all params
            if dependencies is not None:
              for dependency in dependencies:
                if getattr(msg.dragonConf, get_struct_name(dependency['name'])) not in dependency['vals']:
                  update_this_conf = False

          if update_this_conf:
            val = params.get(conf['name'], encoding='utf8')
            if val is not None:
              val = val.rstrip('\x00')
            struct_val = to_struct_val(conf['name'], val)
            orig_val = struct_val
            if conf.get('min') is not None:
              struct_val = max(struct_val, conf.get('min'))
            if conf.get('max') is not None:
              struct_val = min(struct_val, conf.get('max'))
            if orig_val != struct_val:
              params.put(conf['name'], str(struct_val))
            setattr(msg.dragonConf, get_struct_name(conf['name']), struct_val)
    '''
    ===================================================
    push ip addr every 10 secs to message
    ===================================================
    '''
    if frame % 20 == 0:
      val = 'N/A'
      try:
        result = subprocess.check_output(["ifconfig", "wlan0"], encoding='utf8')
        val = re.findall(r"inet addr:((\d+\.){3}\d+)", result)[0][0]
      except:
        pass
      msg.dragonConf.dpIpAddr = val
    '''
    ===================================================
    push is_updating status every 5 secs to message
    ===================================================
    '''
    if frame % 10 == 0:
      val = params.get('dp_is_updating', encoding='utf8').rstrip('\x00')
      setattr(msg.dragonConf, get_struct_name('dp_is_updating'), to_struct_val('dp_is_updating', val))
    '''
    ===================================================
    push once
    ===================================================
    '''
    if frame == 0:
      setattr(msg.dragonConf, get_struct_name('dp_locale'), locale)
      put_nonblocking('dp_is_updating', '0')
    '''
    ===================================================
    we can have some logic here
    ===================================================
    '''
    if msg.dragonConf.dpAssistedLcMinMph > msg.dragonConf.dpAutoLcMinMph:
      put_nonblocking('dp_auto_lc_min_mph', str(msg.dragonConf.dpAssistedLcMinMph))
      msg.dragonConf.dpAutoLcMinMph = msg.dragonConf.dpAssistedLcMinMph
    if msg.dragonConf.dpAtl:
      msg.dragonConf.dpAllowGas = True
      msg.dragonConf.dpDynamicFollow = 0
      msg.dragonConf.dpAccelProfile = 0
      msg.dragonConf.dpGearCheck = False
    if msg.dragonConf.dpAppWaze:
      msg.dragonConf.dpDrivingUi = False
    if not msg.dragonConf.dpDriverMonitor:
      msg.dragonConf.dpUiFace = False

    msg.dragonConf.dpThermalStarted = started
    msg.dragonConf.dpThermalOverheat = overheat
    '''
    ===================================================
    publish msg
    ===================================================
    '''
    last_dp_msg = msg.dragonConf
    update_params = False
    pm.send('dragonConf', msg)
    '''
    ===================================================
    hotspot on boot
    we do it after 30 secs just in case
    ===================================================
    '''
    if frame == 60 and params.get("dp_hotspot_on_boot") == b'1':
      os.system("service call wifi 37 i32 0 i32 1 &")
    '''
    ===================================================
    dashcam
    ===================================================    
    '''
    dashcam = msg.dragonConf.dpDashcam
    if frame % 2 == 0 and dashcam:
      while not dashcam_folder_exists and dashcam_mkdir_retry <= 5:
        try:
          if not os.path.exists(DASHCAM_VIDEOS_PATH):
            os.makedirs(DASHCAM_VIDEOS_PATH)
          else:
            dashcam_folder_exists = True
        except OSError:
          pass
        dashcam_mkdir_retry += 1
      if dashcam_folder_exists:
        if started:
          if frame >= dashcam_next_frame - 2:
            now = datetime.datetime.now()
            file_name = now.strftime("%Y-%m-%d_%H-%M-%S")
            os.system("screenrecord --bit-rate %s --time-limit %s %s%s.mp4 &" % (DASHCAM_BIT_RATES, DASHCAM_DURATION, DASHCAM_VIDEOS_PATH, file_name))
            dashcam_next_frame = frame + DASHCAM_DURATION*2
        else:
          dashcam_next_frame = 0

        if frame % 120 == 0 and ((free_space < DASHCAM_FREESPACE_LIMIT) or (get_used_spaces() > DASHCAM_KEPT)):
          try:
            files = [f for f in sorted(os.listdir(DASHCAM_VIDEOS_PATH)) if os.path.isfile(DASHCAM_VIDEOS_PATH + f)]
            os.system("rm -fr %s &" % (DASHCAM_VIDEOS_PATH + files[0]))
          except (IndexError, FileNotFoundError, OSError):
            pass
    '''
    ===================================================
    auto shutdown
    ===================================================
    '''
    autoshutdown = msg.dragonConf.dpAutoShutdown
    if frame % 20 == 0 and autoshutdown:
      sec = msg.dragonConf.dpAutoShutdownIn * 60 * 2
      if last_autoshutdown != autoshutdown or last_sec != sec or started or online:
        autoshutdown_frame = frame + sec
      if not started and not online and sec > 0 and frame >= autoshutdown_frame:
        os.system('LD_LIBRARY_PATH="" svc power shutdown')
      last_sec = sec
    last_autoshutdown = autoshutdown
    '''
    ===================================================
    battery ctrl every 30 secs
    PowerMonitor in thermald turns back on every mins
    so lets turn it off more frequent
    ===================================================
    '''
    charging_ctrl = msg.dragonConf.dpChargingCtrl
    if last_charging_ctrl != charging_ctrl:
      set_battery_charging(True)
    if charging_ctrl and frame % 60 == 0:
      if battery_percent >= msg.dragonConf.dpDischargingAt and get_battery_charging():
        set_battery_charging(False)
      elif battery_percent <= msg.dragonConf.dpChargingAt and not get_battery_charging():
        set_battery_charging(True)
    last_charging_ctrl = charging_ctrl
    '''
    ===================================================
    make it 2 hz
    ===================================================
    '''
    last_started = started
    frame += 1
    sleep = 0.5-(sec_since_boot() - start_sec)
    if sleep > 0:
      time.sleep(sleep)

def get_used_spaces():
  return sum(os.path.getsize(DASHCAM_VIDEOS_PATH + f) for f in os.listdir(DASHCAM_VIDEOS_PATH) if os.path.isfile(DASHCAM_VIDEOS_PATH + f))

def main():
  confd_thread()

if __name__ == "__main__":
  main()
