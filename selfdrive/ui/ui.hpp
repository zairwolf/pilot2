#ifndef _UI_H
#define _UI_H
#include "cereal/gen/cpp/log.capnp.h"
#ifdef __APPLE__
#include <OpenGL/gl3.h>
#define NANOVG_GL3_IMPLEMENTATION
#define nvgCreate nvgCreateGL3
#else
#include <GLES3/gl3.h>
#include <EGL/egl.h>
#define NANOVG_GLES3_IMPLEMENTATION
#define nvgCreate nvgCreateGLES3
#endif

#include <capnp/serialize.h>
#include <pthread.h>
#include "nanovg.h"

#include "common/mat.h"
#include "common/visionipc.h"
#include "common/visionimg.h"
#include "common/framebuffer.h"
#include "common/modeldata.h"
#include "messaging.hpp"
#include "sound.hpp"

#define STATUS_STOPPED 0
#define STATUS_DISENGAGED 1
#define STATUS_ENGAGED 2
#define STATUS_WARNING 3
#define STATUS_ALERT 4

#define NET_CONNECTED 0
#define NET_DISCONNECTED 1
#define NET_ERROR 2

#define COLOR_BLACK nvgRGBA(0, 0, 0, 255)
#define COLOR_BLACK_ALPHA(x) nvgRGBA(0, 0, 0, x)
#define COLOR_WHITE nvgRGBA(255, 255, 255, 255)
#define COLOR_WHITE_ALPHA(x) nvgRGBA(255, 255, 255, x)
#define COLOR_YELLOW nvgRGBA(218, 202, 37, 255)
#define COLOR_RED nvgRGBA(201, 34, 49, 255)
#define COLOR_RED_ALPHA(x) nvgRGBA(201, 34, 49, x)
#define COLOR_OCHRE nvgRGBA(218, 111, 37, 255)

#ifndef QCOM
  #define UI_60FPS
#endif

#define UI_BUF_COUNT 4
//#define SHOW_SPEEDLIMIT 1
//#define DEBUG_TURN

const int vwp_w = 1920;
const int vwp_h = 1080;
const int nav_w = 640;
const int nav_ww= 760;
const int sbr_w = 300;
const int bdr_s = 30;
const int box_x = sbr_w+bdr_s;
const int box_y = bdr_s;
const int box_w = vwp_w-sbr_w-(bdr_s*2);
const int box_h = vwp_h-(bdr_s*2);
const int viz_w = vwp_w-(bdr_s*2);
const int ff_xoffset = 32;
const int header_h = 420;
const int footer_h = 280;
const int footer_y = vwp_h-bdr_s-footer_h;
const int settings_btn_h = 117;
const int settings_btn_w = 200;
const int settings_btn_x = 50;
const int settings_btn_y = 35;
const int home_btn_h = 180;
const int home_btn_w = 180;
const int home_btn_x = 60;
const int home_btn_y = vwp_h - home_btn_h - 40;

// dp
// dynamic follow btn
const int df_btn_h = 180;
const int df_btn_w = 180;
const int df_btn_x = 1650;
const int df_btn_y = 750;
// accel profile btn
const int ap_btn_h = 180;
const int ap_btn_w = 180;
const int ap_btn_x = 1450;
const int ap_btn_y = 750;

const int UI_FREQ = 30;   // Hz

const int MODEL_PATH_MAX_VERTICES_CNT = 98;
const int MODEL_LANE_PATH_CNT = 3;
const int TRACK_POINTS_MAX_CNT = 50 * 2;

const int SET_SPEED_NA = 255;

const uint8_t bg_colors[][4] = {
  [STATUS_STOPPED] = {0x07, 0x23, 0x39, 0xff},
  [STATUS_DISENGAGED] = {0x17, 0x33, 0x49, 0xff},
  [STATUS_ENGAGED] = {0x17, 0x86, 0x44, 0xff},
  [STATUS_WARNING] = {0xDA, 0x6F, 0x25, 0xff},
  [STATUS_ALERT] = {0xC9, 0x22, 0x31, 0xff},
};


typedef struct UIScene {
  int frontview;
  int fullview;

  int transformed_width, transformed_height;

  ModelData model;

  float mpc_x[50];
  float mpc_y[50];

  bool world_objects_visible;
  mat4 extrinsic_matrix;      // Last row is 0 so we can use mat4.

  float v_cruise;
  uint64_t v_cruise_update_ts;
  float v_ego;
  bool decel_for_model;

  float speedlimit;
  bool speedlimit_valid;
  bool map_valid;

  float curvature;
  int engaged;
  bool engageable;
  bool monitoring_active;

  bool uilayout_sidebarcollapsed;
  bool uilayout_mapenabled;
  bool uilayout_mockengaged;
  // responsive layout
  int ui_viz_rx;
  int ui_viz_rw;
  int ui_viz_ro;

  int lead_status;
  float lead_d_rel, lead_y_rel, lead_v_rel;

  int lead_status2;
  float lead_d_rel2, lead_y_rel2, lead_v_rel2;

  float face_prob;
  bool is_rhd;
  float face_x, face_y;

  int front_box_x, front_box_y, front_box_width, front_box_height;

  uint64_t alert_ts;
  std::string alert_text1;
  std::string alert_text2;
  cereal::ControlsState::AlertSize alert_size;
  float alert_blinkingrate;

  float awareness_status;

  // Used to show gps planner status
  bool gps_planner_active;

  cereal::ThermalData::NetworkType networkType;
  cereal::ThermalData::NetworkStrength networkStrength;
  int batteryPercent;
  bool batteryCharging;
  float freeSpace;
  cereal::ThermalData::ThermalStatus thermalStatus;
  int paTemp;
  cereal::HealthData::HwType hwType;
  int satelliteCount;
  uint8_t athenaStatus;

  // dp
  // for minimal UI
  float angleSteersDes;
  float angleSteers;
  char ipAddr[20];
  int alert_rate;
  int alert_type;
  // for black screen on reversing
  bool isReversing;

  // for blinker, from kegman
  bool leftBlinker;
  bool rightBlinker;
  bool brakeLights;
  int blinker_blinkingrate;

} UIScene;

typedef struct {
  float x, y;
}vertex_data;

typedef struct {
  vertex_data v[MODEL_PATH_MAX_VERTICES_CNT];
  int cnt;
} model_path_vertices_data;

typedef struct {
  vertex_data v[TRACK_POINTS_MAX_CNT];
  int cnt;
} track_vertices_data;


typedef struct UIState {
  pthread_mutex_t lock;

  // framebuffer
  FramebufferState *fb;
  int fb_w, fb_h;

  // NVG
  NVGcontext *vg;

  // fonts and images
  int font_courbd;
  int font_sans_regular;
  int font_sans_semibold;
  int font_sans_bold;
  int img_wheel;
  int img_turn;
  int img_face;
  int img_map;
  int img_button_settings;
  int img_button_home;
  int img_battery;
  int img_battery_charging;
  int img_network[6];

  // sockets
  Context *ctx;
  SubSocket *model_sock;
  SubSocket *controlsstate_sock;
  SubSocket *livecalibration_sock;
  SubSocket *radarstate_sock;
  SubSocket *map_data_sock;
  SubSocket *uilayout_sock;
  SubSocket *thermal_sock;
  SubSocket *health_sock;
  SubSocket *ubloxgnss_sock;
  SubSocket *driverstate_sock;
  SubSocket *dmonitoring_sock;
  PubSocket *offroad_sock;
  Poller * poller;
  Poller * ublox_poller;

  cereal::UiLayoutState::App active_app;

  // vision state
  bool vision_connected;
  bool vision_connect_firstrun;
  int ipc_fd;

  VIPCBuf bufs[UI_BUF_COUNT];
  VIPCBuf front_bufs[UI_BUF_COUNT];
  int cur_vision_idx;
  int cur_vision_front_idx;

  GLuint frame_program;
  GLuint frame_texs[UI_BUF_COUNT];
  EGLImageKHR khr[UI_BUF_COUNT];
  void *priv_hnds[UI_BUF_COUNT];
  GLuint frame_front_texs[UI_BUF_COUNT];
  EGLImageKHR khr_front[UI_BUF_COUNT];
  void *priv_hnds_front[UI_BUF_COUNT];

  GLint frame_pos_loc, frame_texcoord_loc;
  GLint frame_texture_loc, frame_transform_loc;

  int rgb_width, rgb_height, rgb_stride;
  size_t rgb_buf_len;
  mat4 rgb_transform;

  int rgb_front_width, rgb_front_height, rgb_front_stride;
  size_t rgb_front_buf_len;

  UIScene scene;
  bool awake;

  // timeouts
  int awake_timeout;
  int volume_timeout;
  int controls_timeout;
  int alert_sound_timeout;
  int speed_lim_off_timeout;
  int is_metric_timeout;
  int longitudinal_control_timeout;
  int limit_set_speed_timeout;
  int hardware_timeout;
  int last_athena_ping_timeout;
  int offroad_layout_timeout;

  bool controls_seen;

  uint64_t last_athena_ping;
  int status;
  bool is_metric;
  bool longitudinal_control;
  bool limit_set_speed;
  float speed_lim_off;
  bool is_ego_over_limit;
  std::string alert_type;
  AudibleAlert alert_sound;
  float alert_blinking_alpha;
  bool alert_blinked;
  bool started;
  bool thermal_started, preview_started;
  bool vision_seen;

  float light_sensor;

  int touch_fd;

  // Hints for re-calculations and redrawing
  bool model_changed;
  bool livempc_or_radarstate_changed;

  GLuint frame_vao[2], frame_vbo[2], frame_ibo[2];
  mat4 rear_frame_mat, front_frame_mat;

  model_path_vertices_data model_path_vertices[MODEL_LANE_PATH_CNT * 2];

  track_vertices_data track_vertices[2];

  // dp
  SubSocket *carstate_sock;
  int dragon_updating_timeout;
  int dragon_last_modified_timeout;

  bool dragon_ui_speed;
  bool dragon_ui_event;
  bool dragon_ui_maxspeed;
  bool dragon_ui_face;
  bool dragon_ui_dev;
  bool dragon_ui_dev_mini;
  bool dragon_enable_dashcam;
  float dragon_ui_volume_boost;
  bool dragon_driving_ui;
  bool dragon_ui_lane;
  bool dragon_ui_lead;
  bool dragon_ui_path;
  bool dragon_ui_blinker;
  bool dragon_waze_mode;
  bool dragon_ui_dm_view;
  bool dragon_updating;
  uint64_t dragon_df_mode;
  uint64_t dragon_ap_mode;
  bool dragon_enable_dm;
  char dragon_locale[20];
  bool dragon_ui_screen_off_reversing;
  char dragon_last_modified[20];
  bool dragon_ui_screen_off_driving;
  uint64_t dragon_ui_brightness;
} UIState;

// API
void ui_draw_vision_alert(UIState *s, cereal::ControlsState::AlertSize va_size, int va_color,
                          const char* va_text1, const char* va_text2);
void ui_draw(UIState *s);
void ui_draw_sidebar(UIState *s);
void ui_draw_image(NVGcontext *vg, float x, float y, float w, float h, int image, float alpha);
void ui_draw_rect(NVGcontext *vg, float x, float y, float w, float h, NVGcolor color, float r = 0, int width = 0);
void ui_draw_rect(NVGcontext *vg, float x, float y, float w, float h, NVGpaint paint, float r = 0);
void ui_nvg_init(UIState *s);

#endif
