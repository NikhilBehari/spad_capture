/*
 * vl53l8ch_cnh.ino  --  spad_capture_st firmware
 * ----------------------------------------------------------------------------
 * Streams Compact Normalized Histogram (CNH) frames from an ST VL53L8CH SPAD
 * ToF sensor (X-NUCLEO-53L8A1 shield) over the ST-LINK USB virtual COM port.
 *
 * Board   : NUCLEO-F401RE  (Arduino core: STMicroelectronics:stm32)
 * Shield  : X-NUCLEO-53L8A1  (VL53L8CH, I2C @ 0x52 8-bit / 0x29 7-bit)
 * Link    : USB CDC (ST-LINK VCP) @ 921600 baud
 *
 * This sketch is the minimal counterpart to spad_center's STM32CubeIDE
 * firmware: it distills that project's CNH configuration + read path
 * (Core/Src/app.c) down to a single, well-commented Arduino sketch and swaps
 * the line-oriented ASCII dump for a compact binary wire format that the host
 * parser.py consumes directly.
 *
 * The CNH (motion-indicator) plugin from the ST VL53LMZ ULD is vendored into
 * this sketch folder (see firmware/README.md). We drive the ULD directly,
 * exactly as spad_center does, rather than going through a wrapper library so
 * the on-the-wire contract stays fully under our control.
 *
 * --------------------------------------------------------------------------
 * SERIAL PROTOCOL  (must stay byte-for-byte in sync with transport.py)
 * --------------------------------------------------------------------------
 *   Control lines are ASCII, '\n'-terminated. Responses are ASCII lines
 *   except streamed data frames, which are binary (see WIRE FORMAT below).
 *   Control bytes are only acted on at frame boundaries, never mid-frame.
 *
 *     PC -> MCU                                          MCU -> PC
 *     --------------------------------------------       ----------------------
 *     I\n                       (identify / is_alive)    STH1 VL53L8CH 1.0 ID=F0\n
 *     C mode sb nb bf freq integ ranging \             (configure)
 *       [agg_start_x agg_start_y agg_cols agg_rows]\n   OK CFG\n | ERR CFG <n>\n
 *     M\n                       (start ranging)          OK START\n  then frames
 *     S\n                       (stop ranging)           OK STOP\n
 *
 *   C-line integers (all derived host-side from physical units; no mm here):
 *     mode     : 1 = 4x4 (16 zones), 2 = 8x8 (64 zones)
 *     sb       : CNH start_bin           (native ~18.77 mm one-way bins)
 *     nb       : CNH num_bins            (after binning, <= 255: uint8 cap)
 *     bf       : CNH binning_factor      (a.k.a. sub-sample)
 *     freq     : ranging_frequency_hz    (1..30, CNH cap is 30 Hz)
 *     integ    : integration_time_ms     (2-1000 ms; effective in autonomous mode only; ignored in continuous - device auto-integrates to the ranging period)
 *     ranging  : 0 = continuous, 1 = autonomous
 *   Optional ROI / aggregate-selection ints (whole grid when omitted):
 *     agg_start_x : ROI start column (= col), 0-based
 *     agg_start_y : ROI start row    (= row), 0-based
 *     agg_cols    : ROI width  (output W = nb_of_aggregates columns)
 *     agg_rows    : ROI height (output H)
 *   The C line accepts EITHER 7 ints (legacy, full grid) OR 11 ints. With 7
 *   ints, or agg_cols/agg_rows = 0, the ROI defaults to the full grid, giving
 *   byte-identical behavior to the 7-int form. Output frame dims H/W below are
 *   the ROI aggregate grid (agg_rows x agg_cols), not the sensor resolution.
 *   Examples:
 *     C 1 0 53 2 15 20 0\n              (4x4 full grid, legacy form)
 *     C 2 0 18 4 10 20 0 0 0 8 8\n      (8x8 full grid, explicit defaults)
 *     C 2 0 128 2 10 20 0 4 3 1 1\n     (8x8 single zone row=3,col=4 -> 1x1)
 *     C 2 0 200 2 10 20 0 4 3 2 2\n     (8x8 2x2 ROI at row=3,col=4)
 *
 * --------------------------------------------------------------------------
 * WIRE FORMAT  (one data frame, little-endian; mirrors parser._FRAME_HEADER)
 * --------------------------------------------------------------------------
 *   offset  type            field
 *   ------  --------------  ---------------------------------------------
 *   0       u8              START_BYTE (0xAA)
 *   1       4 bytes         MAGIC "STH1"
 *   5       u32             frame_index        (monotonic)
 *   9       u32             device_ts_ms       (millis() at emit)
 *   13      u8              mode_code          (1=4x4, 2=8x8; sensor grid)
 *   14      u16             height (H)         (ROI aggregate rows = agg_rows)
 *   16      u16             width  (W)         (ROI aggregate cols = agg_cols)
 *   18      u16             num_bins (B)
 *   20      f32 * (H*W*B)   histogram, zone-major then bin (zone z = r*W + c)
 *   ...     f32 * (H*W)     ambient,   one per zone (zone-major)
 *   ...     u32             crc32 (IEEE/zlib) over bytes [1 .. crc_start)
 *   ...     u8              END_BYTE (0x55)
 *
 *   "Zone-major" matches host reshape histogram[r, c, :] for zone r*W + c.
 *   H/W are the ROI output dimensions (== nb_of_aggregates rows/cols); for a
 *   full-grid config they equal the sensor resolution (4x4 -> 4,4; 8x8 -> 8,8).
 * ----------------------------------------------------------------------------
 */

#include <Arduino.h>
#include <Wire.h>

extern "C" {
#include "vl53lmz_api.h"
#include "vl53lmz_plugin_cnh.h"
}

/* ===========================================================================
 * Protocol / wire constants  (keep identical to config.py)
 * ========================================================================= */
static const uint8_t  START_BYTE = 0xAA;
static const uint8_t  END_BYTE   = 0x55;
static const char     MAGIC[4]   = {'S', 'T', 'H', '1'};
static const uint8_t  FW_MAJOR   = 1;
static const uint8_t  FW_MINOR   = 0;
static const uint32_t SERIAL_BAUD = 921600;

/* I2C clock for the shield. 1 MHz (FM+) if the bus is wired for it, else the
 * standard fast-mode 400 kHz. NUCLEO-F401RE Arduino headers do FM+ reliably. */
static const uint32_t I2C_CLOCK_HZ = 1000000;

/* On-device CNH persistent-buffer budget (must match MEM_BUDGET_BYTES). The
 * host validates this too; we re-check here so a bad C line is rejected, not
 * silently truncated by the sensor. */
static const uint32_t MEM_BUDGET_BYTES = 6160;

/* ===========================================================================
 * Compile-time defaults  (mirror configs/default.yaml: 4x4, 0..2000 mm,
 * bin 37 mm -> binning_factor 2, 53 bins, 15 Hz, 20 ms, continuous).
 * The C command overrides these at runtime before M.
 * ========================================================================= */
#define DEFAULT_MODE_CODE        1     /* 1 = 4x4, 2 = 8x8           */
#define DEFAULT_START_BIN        0
#define DEFAULT_NUM_BINS         53
#define DEFAULT_BINNING_FACTOR   2
#define DEFAULT_FREQ_HZ          15
#define DEFAULT_INTEG_MS         20
#define DEFAULT_RANGING          0     /* 0 = continuous, 1 = autonomous */

/* ===========================================================================
 * Runtime configuration (the 7-or-11 C-line integers, validated + applied).
 * ========================================================================= */
struct Cfg {
  uint16_t mode_code;       /* 1 = 4x4, 2 = 8x8                    */
  uint16_t resolution;      /* 16 or 64 (zones), derived from mode */
  uint16_t height;          /* 4 or 8                              */
  uint16_t width;           /* 4 or 8                              */
  uint16_t start_bin;
  uint16_t num_bins;
  uint16_t binning_factor;  /* CNH sub-sample factor               */
  uint16_t freq_hz;
  uint16_t integ_ms;
  uint16_t ranging_mode;    /* VL53LMZ_RANGING_MODE_* (1 or 3)     */
  /* ROI / aggregate selection (from the C-line; default = full grid). */
  uint16_t agg_start_x;     /* = col  */
  uint16_t agg_start_y;     /* = row  */
  uint16_t agg_cols;        /* output W */
  uint16_t agg_rows;        /* output H */
};

/* ===========================================================================
 * Globals
 * ========================================================================= */
static VL53LMZ_Configuration         Dev;          /* sensor handle           */
static VL53LMZ_Motion_Configuration  cnh_config;   /* CNH/aggregate config    */
static uint32_t                      cnh_data_size; /* bytes the sensor emits */

static Cfg      cfg;
static bool     ranging   = false;   /* true once M has started streaming  */
static uint8_t  device_id = 0;       /* vl53lmz_is_alive() id byte         */
static uint32_t frame_index = 0;

/* Reusable buffers (no per-frame heap churn). */
static cnh_data_buffer_t cnh_data_buffer;                 /* raw CNH from sensor */
static float hist_buf[VL53LMZ_RESOLUTION_8X8 * 128];      /* H*W*B float32       */
static float amb_buf[VL53LMZ_RESOLUTION_8X8];             /* H*W   float32       */
static char  line[96];                                    /* control-line buffer */

/* ===========================================================================
 * CRC-32 (IEEE 802.3, reflected) -- matches Python zlib.crc32 / parser.py.
 * Computed incrementally so we never need the whole frame in one buffer.
 * ========================================================================= */
static uint32_t crc;

static inline void crc_begin() { crc = 0xFFFFFFFFUL; }

static inline void crc_update(uint8_t b) {
  crc ^= b;
  for (uint8_t k = 0; k < 8; k++)
    crc = (crc >> 1) ^ (0xEDB88320UL & (-(int32_t)(crc & 1)));
}

static inline uint32_t crc_final() { return crc ^ 0xFFFFFFFFUL; }

/* ===========================================================================
 * Serial emit helpers. Every payload byte (everything after START_BYTE and
 * before the trailing CRC) is folded into the running CRC as it is written.
 * ========================================================================= */
static inline void emit(uint8_t b)            { Serial.write(b); }
static inline void emit_crc(uint8_t b)        { crc_update(b); Serial.write(b); }

static void emit_u16_crc(uint16_t v) {        /* little-endian */
  emit_crc((uint8_t)(v & 0xFF));
  emit_crc((uint8_t)(v >> 8));
}

static void emit_u32_crc(uint32_t v) {        /* little-endian */
  emit_crc((uint8_t)(v & 0xFF));
  emit_crc((uint8_t)((v >> 8) & 0xFF));
  emit_crc((uint8_t)((v >> 16) & 0xFF));
  emit_crc((uint8_t)((v >> 24) & 0xFF));
}

static void emit_f32_crc(float f) {           /* IEEE-754 little-endian */
  uint8_t b[4];
  memcpy(b, &f, 4);
  emit_crc(b[0]); emit_crc(b[1]); emit_crc(b[2]); emit_crc(b[3]);
}

/* Trailing CRC field: emitted raw (little-endian) and NOT folded back in. */
static void emit_u32_raw(uint32_t v) {
  emit((uint8_t)(v & 0xFF));
  emit((uint8_t)((v >> 8) & 0xFF));
  emit((uint8_t)((v >> 16) & 0xFF));
  emit((uint8_t)((v >> 24) & 0xFF));
}

/* ===========================================================================
 * Defaults / validation
 * ========================================================================= */
static void cfg_defaults() {
  cfg.mode_code      = DEFAULT_MODE_CODE;
  cfg.start_bin      = DEFAULT_START_BIN;
  cfg.num_bins       = DEFAULT_NUM_BINS;
  cfg.binning_factor = DEFAULT_BINNING_FACTOR;
  cfg.freq_hz        = DEFAULT_FREQ_HZ;
  cfg.integ_ms       = DEFAULT_INTEG_MS;
  cfg.ranging_mode   = (DEFAULT_RANGING == 1) ? VL53LMZ_RANGING_MODE_AUTONOMOUS
                                              : VL53LMZ_RANGING_MODE_CONTINUOUS;
}

/* Fill the derived geometry fields from c.mode_code; return false if invalid. */
static bool cfg_derive_geometry(Cfg &c) {
  if (c.mode_code == 1) {
    c.resolution = VL53LMZ_RESOLUTION_4X4; c.height = 4; c.width = 4;
  } else if (c.mode_code == 2) {
    c.resolution = VL53LMZ_RESOLUTION_8X8; c.height = 8; c.width = 8;
  } else {
    return false;
  }
  /* Default ROI = full grid; handle_configure overrides when 11 ints given. */
  c.agg_start_x = 0; c.agg_start_y = 0;
  c.agg_cols = c.width; c.agg_rows = c.height;
  return true;
}

/* Exact on-device buffer size (DISABLE_PING_PONG + DISABLE_VARIANCE):
 *   per_buf = 8 + axf*4 + ceil4(axf) + nb_agg*4 + ceil4(nb_agg)
 *   total   = per_buf + 20            (single buffer, no variance)
 * ceil4(n) = ((3+n)/4)*4. Mirrors _cnh_calculate_required_memory(). The budget
 * is computed on the aggregate count (agg_cols*agg_rows), not the sensor grid;
 * for the full-grid case (nb_agg in {16,64}) this returns the same bytes as the
 * old zones*num_bins*5 + zones*5 + 28 formula, so the legacy path is unchanged.
 * Also enforces the 1..30 Hz CNH cap, num_bins/binning_factor sanity, the uint8
 * feature_length cap (num_bins <= 255), and the ROI grid bounds. */
static bool cfg_validate(const Cfg &c, int &reason) {
  if (c.freq_hz < 1 || c.freq_hz > 30) { reason = 1; return false; }
  if (c.integ_ms == 0)                 { reason = 2; return false; }
  if (c.num_bins == 0)                 { reason = 3; return false; }
  if (c.binning_factor == 0)           { reason = 4; return false; }
  /* feature_length is uint8 on-device. */
  if (c.num_bins > 255)                { reason = 6; return false; }

  /* ROI bounds: must lie inside the sensor grid (merge_x=merge_y=1). */
  if (c.agg_cols == 0 || c.agg_rows == 0)                  { reason = 7; return false; }
  if ((uint32_t)c.agg_start_x + c.agg_cols > c.width)      { reason = 7; return false; }
  if ((uint32_t)c.agg_start_y + c.agg_rows > c.height)     { reason = 7; return false; }

  uint32_t nb_agg = (uint32_t)c.agg_cols * (uint32_t)c.agg_rows;  /* <= 64 */
  uint32_t axf    = nb_agg * (uint32_t)c.num_bins;
  uint32_t per_buf = 8u
                   + axf * 4u + ((3u + axf) / 4u) * 4u
                   + nb_agg * 4u + ((3u + nb_agg) / 4u) * 4u;
  uint32_t total = per_buf + 20u;
  if (total > MEM_BUDGET_BYTES)        { reason = 5; return false; }
  return true;
}

/* ===========================================================================
 * Sensor bring-up: init, basic ranging settings, CNH config, custom output
 * block, start ranging. Distilled from spad_center Core/Src/app.c.
 * Returns VL53LMZ_STATUS_OK (0) on success.
 * ========================================================================= */
static uint8_t apply_config_and_start() {
  uint8_t status, alive = 0;

  status = vl53lmz_is_alive(&Dev, &alive);
  if (status || !alive) return status ? status : (uint8_t)255;
  device_id = alive;                 /* report whatever the part returns */

  status = vl53lmz_init(&Dev);                                if (status) return status;
  status = vl53lmz_set_resolution(&Dev, (uint8_t)cfg.resolution);          if (status) return status;
  status = vl53lmz_set_ranging_mode(&Dev, (uint8_t)cfg.ranging_mode);      if (status) return status;
  status = vl53lmz_set_ranging_frequency_hz(&Dev, (uint8_t)cfg.freq_hz);   if (status) return status;
  status = vl53lmz_set_integration_time_ms(&Dev, cfg.integ_ms);           if (status) return status;

  /* --- CNH: histogram window + one aggregate per zone (no merging) --- */
  status = vl53lmz_cnh_init_config(&cnh_config,
                                   (int16_t)cfg.start_bin,
                                   (int16_t)cfg.num_bins,
                                   (int16_t)cfg.binning_factor);
  if (status) return status;

  /* agg map: ROI (or full grid by default), 1:1 zone->aggregate. */
  status = vl53lmz_cnh_create_agg_map(&cnh_config,
                                      (int16_t)cfg.resolution,
                                      /* start_x */ (int16_t)cfg.agg_start_x,  /* = col */
                                      /* start_y */ (int16_t)cfg.agg_start_y,  /* = row */
                                      /* merge_x */ 1, /* merge_y */ 1,
                                      /* cols    */ (int16_t)cfg.agg_cols,
                                      /* rows    */ (int16_t)cfg.agg_rows);
  if (status) return status;

  status = vl53lmz_cnh_calc_required_memory(&cnh_config, &cnh_data_size);  if (status) return status;
  status = vl53lmz_cnh_send_config(&Dev, &cnh_config);                     if (status) return status;

  /* Non-standard transfer: build the output config, then append the CNH
   * block so only the histogram data is streamed back (see app.c). */
  status = vl53lmz_create_output_config(&Dev);                             if (status) return status;

  union Block_header cnh_bh;
  cnh_bh.idx  = VL53LMZ_CNH_DATA_IDX;
  cnh_bh.type = 4;
  cnh_bh.size = cnh_data_size / 4;
  status = vl53lmz_add_output_block(&Dev, cnh_bh.bytes);                   if (status) return status;

  vl53lmz_set_sharpener_percent(&Dev, 0);                 /* no sharpening */

  status = vl53lmz_send_output_config_and_start(&Dev);                     if (status) return status;
  return VL53LMZ_STATUS_OK;
}

/* ===========================================================================
 * Read one ready frame from the sensor, decode CNH to float32 magnitudes,
 * and stream it in the §4 wire format. Called only while ranging.
 *
 * CNH decode (per the ULD CNH plugin, matching app.c):
 *   value   = (float)p_hist[i]  / (1 << p_hist_scaler[i])
 *   ambient = (float)*p_ambient / (1 << *p_ambient_scaler)
 * The scaler is a right-shift divisor (exponent-mantissa form); we expand it
 * here so the host receives plain float32 physical magnitudes.
 * ========================================================================= */
static void stream_one_frame() {
  uint8_t isReady = 0;
  if (vl53lmz_check_data_ready(&Dev, &isReady) != VL53LMZ_STATUS_OK || !isReady)
    return;

  VL53LMZ_ResultsData results;
  if (vl53lmz_get_ranging_data(&Dev, &results) != VL53LMZ_STATUS_OK) return;

  if (vl53lmz_results_extract_block(&Dev, VL53LMZ_CNH_DATA_IDX,
                                    (uint8_t *)cnh_data_buffer,
                                    cnh_data_size) != VL53LMZ_STATUS_OK)
    return;

  /* OUTPUT dims = ROI aggregate grid (NOT the sensor resolution). */
  const uint16_t H = cfg.agg_rows, W = cfg.agg_cols, B = cfg.num_bins;
  const uint16_t num_agg = H * W;            /* == cnh_config.nb_of_aggregates */

  /* Decode every aggregate into the contiguous float buffers. Aggregate ids
   * are row-major within the ROI (agg_id formula in vl53lmz_plugin_cnh.c:188):
   * z=0 is ROI top-left (row=start_y,col=start_x), z increases across cols then
   * rows. For a single zone z=0 is that zone. */
  int32_t *p_hist = NULL, *p_ambient = NULL;
  int8_t  *p_hist_scaler = NULL, *p_ambient_scaler = NULL;

  for (uint16_t z = 0; z < num_agg && z < cnh_config.nb_of_aggregates; z++) {
    vl53lmz_cnh_get_block_addresses(&cnh_config, z, cnh_data_buffer,
                                    &p_hist, &p_hist_scaler,
                                    &p_ambient, &p_ambient_scaler);

    amb_buf[z] = (float)(*p_ambient) / (float)(1 << *p_ambient_scaler);

    float *dst = &hist_buf[(uint32_t)z * B];
    for (uint16_t b = 0; b < B; b++)
      dst[b] = (float)p_hist[b] / (float)(1 << p_hist_scaler[b]);
  }

  /* ---- emit the framed packet ---- */
  emit(START_BYTE);
  crc_begin();                                  /* CRC covers [1 .. crc) */
  for (uint8_t i = 0; i < 4; i++) emit_crc((uint8_t)MAGIC[i]);

  emit_u32_crc(frame_index);
  emit_u32_crc((uint32_t)millis());
  emit_crc((uint8_t)cfg.mode_code);          /* sensor resolution (context only) */
  emit_u16_crc(H);                           /* ROI height = agg_rows */
  emit_u16_crc(W);                           /* ROI width  = agg_cols */
  emit_u16_crc(B);

  for (uint32_t i = 0; i < (uint32_t)num_agg * B; i++) emit_f32_crc(hist_buf[i]);
  for (uint16_t z = 0; z < num_agg; z++)               emit_f32_crc(amb_buf[z]);

  emit_u32_raw(crc_final());                    /* trailing CRC (not CRC'd) */
  emit(END_BYTE);

  frame_index++;
}

/* ===========================================================================
 * Control-line handling.  poll_control() reads at most one '\n'-terminated
 * ASCII line per call (non-blocking) and dispatches it. Binary data frames are
 * never interleaved with a line read, so control bytes act only at boundaries.
 * ========================================================================= */
static void handle_identify() {
  /* STH1 VL53L8CH <maj>.<min> ID=<u8 hex> */
  snprintf(line, sizeof(line), "STH1 VL53L8CH %u.%u ID=%02X",
           (unsigned)FW_MAJOR, (unsigned)FW_MINOR, (unsigned)device_id);
  Serial.println(line);
}

static void handle_configure(const char *args) {
  /* args = "mode sb nb bf freq integ ranging [agg_start_x agg_start_y agg_cols agg_rows]"
   * The 4 trailing ROI ints are optional: a legacy 7-int host omits them and
   * gets the full grid; the new host always sends 11 (full-grid runs send the
   * 0 0 W H defaults, reproducing the legacy create_agg_map call exactly). */
  int mode, sb, nb, bf, freq, integ, rng;
  int asx = 0, asy = 0, acols = 0, arows = 0;     /* 0 => full-grid default */
  int n = sscanf(args, "%d %d %d %d %d %d %d %d %d %d %d",
                 &mode, &sb, &nb, &bf, &freq, &integ, &rng,
                 &asx, &asy, &acols, &arows);
  if (n != 7 && n != 11) { Serial.println("ERR CFG parse"); return; }

  Cfg c;
  c.mode_code      = (uint16_t)mode;
  c.start_bin      = (uint16_t)sb;
  c.num_bins       = (uint16_t)nb;
  c.binning_factor = (uint16_t)bf;
  c.freq_hz        = (uint16_t)freq;
  c.integ_ms       = (uint16_t)integ;
  c.ranging_mode   = (rng == 1) ? VL53LMZ_RANGING_MODE_AUTONOMOUS
                                : VL53LMZ_RANGING_MODE_CONTINUOUS;

  /* derive geometry (also seeds full-grid agg defaults), then validate */
  if (!cfg_derive_geometry(c)) { Serial.println("ERR CFG mode"); return; }
  /* Override the full-grid agg defaults only when the 4 ROI ints are given. */
  if (n == 11) {
    c.agg_start_x = (uint16_t)asx;
    c.agg_start_y = (uint16_t)asy;
    c.agg_cols    = (acols > 0) ? (uint16_t)acols : c.width;
    c.agg_rows    = (arows > 0) ? (uint16_t)arows : c.height;
  }

  int reason = 0;
  if (!cfg_validate(c, reason)) {
    snprintf(line, sizeof(line), "ERR CFG %d", reason);
    Serial.println(line);
    return;
  }

  cfg = c;                                       /* accept the new config */
  Serial.println("OK CFG");
}

static void handle_start() {
  if (!ranging) {
    uint8_t st = apply_config_and_start();
    if (st != VL53LMZ_STATUS_OK) {
      snprintf(line, sizeof(line), "ERR START %d", (int)st);
      Serial.println(line);
      return;
    }
    ranging = true;
    frame_index = 0;
  }
  Serial.println("OK START");
}

static void handle_stop() {
  if (ranging) {
    vl53lmz_stop_ranging(&Dev);
    ranging = false;
  }
  Serial.println("OK STOP");
}

/* Pull one control line if present and dispatch it. */
static void poll_control() {
  if (!Serial.available()) return;

  size_t len = Serial.readBytesUntil('\n', line, sizeof(line) - 1);
  if (len == 0) return;
  line[len] = '\0';
  /* trim a trailing CR if the host sent CRLF */
  if (len && line[len - 1] == '\r') line[len - 1] = '\0';

  switch (line[0]) {
    case 'I': handle_identify();      break;
    case 'C': handle_configure(line + 1); break;   /* skip the 'C' */
    case 'M': handle_start();         break;
    case 'S': handle_stop();          break;
    default: /* ignore unknown / stray bytes */    break;
  }
}

/* ===========================================================================
 * Arduino entry points
 * ========================================================================= */
/* X-NUCLEO-53L8A1 power-up. The shield's enable lines must be driven before the
 * VL53L8CH answers on I2C. Pins match the spad_center reference firmware:
 *   SPI_I2C_N = PC13 (low selects I2C), PWR_EN = PA7, LPn = PB0, AVDD_EN = PB1. */
static void power_up_sensor() {
  pinMode(PC13, OUTPUT); digitalWrite(PC13, LOW);   /* select I2C interface */
  pinMode(PB1,  OUTPUT); digitalWrite(PB1,  HIGH);  /* AVDD enable          */
  pinMode(PB0,  OUTPUT); digitalWrite(PB0,  LOW);   /* LPn low (reset)      */
  pinMode(PA7,  OUTPUT); digitalWrite(PA7,  LOW);   /* PWR_EN off           */
  delay(100);
  digitalWrite(PA7, HIGH);                          /* PWR_EN on            */
  delay(100);
  digitalWrite(PB0, HIGH);                          /* LPn high (enable)    */
  digitalWrite(PC13, LOW);  delay(2);               /* I2C bus clear toggle */
  digitalWrite(PC13, HIGH); delay(2);
  digitalWrite(PC13, LOW);  delay(2);
  delay(10);
}

void setup() {
  Serial.begin(SERIAL_BAUD);

  power_up_sensor();

  Wire.begin();
  Wire.setClock(I2C_CLOCK_HZ);

  /* Bind the sensor handle to the shield's default I2C address. The vendored
   * platform layer (platform.cpp) routes RdByte/WrByte/etc. through Wire. */
  Dev.platform.address = VL53LMZ_DEFAULT_I2C_ADDRESS;

  cfg_defaults();
  cfg_derive_geometry(cfg);

  /* Probe the part once so `I` (is_alive) works before the first `M`. A
   * missing/asleep sensor simply leaves device_id = 0; the host treats a
   * present STH1 banner as "alive" and reads ID for diagnostics. */
  uint8_t alive = 0;
  if (vl53lmz_is_alive(&Dev, &alive) == VL53LMZ_STATUS_OK) device_id = alive;
}

void loop() {
  /* Control bytes are honoured between frames so I/S work mid-stream. */
  poll_control();

  if (ranging) stream_one_frame();
}
