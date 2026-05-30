/*
 *****************************************************************************
 * Copyright by ams OSRAM AG                                                 *
 * All rights are reserved.                                                  *
 *                                                                           *
 * IMPORTANT - PLEASE READ CAREFULLY BEFORE COPYING, INSTALLING OR USING     *
 * THE SOFTWARE.                                                             *
 *                                                                           *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS       *
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT         *
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS         *
 * FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT  *
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,     *
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT          *
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,     *
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY     *
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT       *
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE     *
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.      *
 *****************************************************************************
 */
//
// tmf8828 arduino uno sample program
//

// ---------------------------------------------- includes
// ----------------------------------------

#include "tmf8828_app.h"
#include "tmf8828.h"
#include "tmf8828_calib.h"
#include "tmf8828_image.h"
#include "tmf8828_shim.h"
#include "tmf882x_calib.h"
#include "tmf882x_image.h"

// ---------------------------------------------- defines
// -----------------------------------------

// tmf states
#define TMF8828_STATE_DISABLED 0
#define TMF8828_STATE_STANDBY 1
#define TMF8828_STATE_STOPPED 2
#define TMF8828_STATE_MEASURE 3
#define TMF8828_STATE_ERROR 4

// number of log-levels in array
#define NR_LOG_LEVELS 7

// number of register that are printed in the dump on one line
#define NR_REGS_PER_LINE 8

// number of TMF8828 instances
#define NR_OF_TMF8828 1

// ---------------------------------------------- constants
// -----------------------------------------

// to increase/decrease logging
const uint8_t logLevels[NR_LOG_LEVELS] = {
    TMF8828_LOG_LEVEL_NONE,           TMF8828_LOG_LEVEL_ERROR,
    TMF8828_LOG_LEVEL_CLK_CORRECTION, TMF8828_LOG_LEVEL_INFO,
    TMF8828_LOG_LEVEL_VERBOSE,        TMF8828_LOG_LEVEL_I2C,
    TMF8828_LOG_LEVEL_DEBUG};

// first dimention of all configurations defines if it is for tmf882x or tmf8828
#define TMF882X_CONFIG_IDX 0
#define TMF8828_CONFIG_IDX 1

// Period in ms; mutable so host can override at runtime via 'P<digits>\n'.
uint16_t configPeriod[2][2] = {
    {16, 8},     // TMF882X
    {132, 264}   // TMF8828
};

// Custom-mask 109-byte payload (TMF882X SPAD-1 config page, cid_rid=0x17).
// Per DS000693 §8.6, registers 0x24..0x90 of page 0x17 hold the user-defined
// SPAD mask. The host uploads via the 'U' command; the firmware does a
// configure() pass first (which sets spad_map_id=14 on the common page) and
// then loads + writes + commits page 0x17.
#define TMF8X2X_MASK_PAYLOAD_BYTES 109
#define TMF8X2X_MASK_ENABLE_REG    0x24  // enableSpad[10] × 3 B (30 B)
#define TMF8X2X_MASK_TDC_REG       0x42  // tdcChannel[18] × 4 B (72 B)
#define TMF8X2X_MASK_TDCSEL_REG    0x8a  // tdcChannelSelect × 3 B
#define TMF8X2X_MASK_TRAILER_REG   0x8d  // xOffset_2 + yOffset_2 + xSize + ySize (4 B)
// CMD_STAT values for the page-load commands (DS000693 §8.4 Table).
#define TMF8X2X_CMD_LOAD_PAGE_SPAD_1     0x17
#define TMF8X2X_CMD_LOAD_PAGE_FAC_CALIB  0x19
#define TMF8X2X_CMD_FACTORY_CALIBRATION  0x20
#define TMF8X2X_CMD_WRITE_PAGE           0x15
#define TMF8X2X_CMD_STAT_REG             0x08
#define TMF8X2X_STAT_REG                 0x08   // status returned in same byte
#define TMF8X2X_CONFIG_RESULT_REG        0x20
// Factory-calibration payload: 188 bytes at register 0x24..0xDF on page 0x19
// (cal page; first 4 bytes 0x20..0x23 are the page header — read-only).
#define TMF8X2X_CAL_PAYLOAD_BYTES        188
#define TMF8X2X_CAL_START_REG            0x24
extern uint8_t dataBuffer[];  // global scratch buffer, declared in tmf8828.c

// Kilo-iterations (Kilo = 1024); mutable, override via 'I<digits>\n'.
uint16_t configKiloIter[2][2] = {{10000, 2500}, {10, 2500}};

// spad_map_id; mutable, override via 'M<digits>\n'.
uint8_t configSpadId[2][2] = {
    {TMF8828_COM_SPAD_MAP_ID__spad_map_id__map_no_6,
     TMF8828_COM_SPAD_MAP_ID__spad_map_id__map_no_7},
    {TMF8828_COM_SPAD_MAP_ID__spad_map_id__map_no_15,
     TMF8828_COM_SPAD_MAP_ID__spad_map_id__map_no_15}
};

// set the lower threshold to 0cm
const uint16_t configLowThreshold = 0;
// set the upper threshold to 500cm
const uint16_t configHighThreshold = 500;
// select perstistence to be: 0==report every distance, even no distance; 1==
// report every distance that is a distance, 3== report distance only if 3x in
// range
const uint8_t configPersistance[3] = {0, 1, 3};
// interrupt selection mask is 18-bits, if bit is set, zone can report an
// interrupt
const uint32_t configInterruptMask = 0x3FFFF;

// ---------------------------------------------- variables
// -----------------------------------------

tmf8828Driver tmf8828[NR_OF_TMF8828]; // instances of tmf8828
uint8_t logLevel;                     // how chatty the program is
int8_t stateTmf8828;                  // current state of the device
int8_t
    modeIsTmf8828; // if set to 1 this is the tmf8828 else this is the tmf882x
int8_t isLongRangeMode; // if set to 1 this is the long range mode, else this is
                        // the short range mode
int8_t configNr;   // this sample application has only a few configurations it
                   // will loop through, the variable keeps track of that
int8_t persistenceNr;   // this is to keep track of the selected persistence
                        // setting (out of three for this sample application)
int8_t clkCorrectionOn; // if non-zero clock correction is on
int8_t dumpHistogramOn; // if non-zero, dump all histograms
uint8_t logLevelIdx;    // log level indes into logLevels array
volatile uint8_t irqTriggered; // interrupt is triggered or not

// ---------------------------------------------- function declaration
// ------------------------------

void printDeviceInfo();
void printHelp();
void printRegisters(uint8_t regAddr, uint16_t len, char seperator, uint8_t calibId);
void resetAppState();
void setMode();
void setAccuracyMode();
void uploadCustomMask();
void configure();

// ---------------------------------------------- functions
// -----------------------------------------

// Read `n` bytes from serial into `buf` with a per-byte deadline. Returns 1 on
// success, 0 if the deadline expires before `n` bytes arrive.
static int8_t readSerialBlocking(uint8_t *buf, uint16_t n, uint16_t per_byte_ms) {
  uint16_t got = 0;
  unsigned long deadline = millis() + per_byte_ms;
  char c;
  while (got < n) {
    if (inputGetKey(&c)) {
      buf[got++] = (uint8_t)c;
      deadline = millis() + per_byte_ms;
    } else if ((long)(millis() - deadline) > 0) {
      return 0;
    }
  }
  return 1;
}

// 'U' command: upload a user-defined SPAD mask and reconfigure the device.
// Wire protocol: 'U' + 1 byte map_id (14 or 15) + 109 bytes mask payload.
// Custom masks only work in TMF882x (legacy) mode per DS000693 §7.4.1, so we
// force a mode switch if needed.
void uploadCustomMask() {
  uint8_t map_id = 0;
  if (!readSerialBlocking(&map_id, 1, 500)) {
    PRINT_CONST_STR(F("#Err U timeout map_id"));
    PRINT_LN();
    return;
  }
  if (map_id != 14 && map_id != 15) {
    PRINT_CONST_STR(F("#Err U bad map_id="));
    PRINT_INT(map_id);
    PRINT_LN();
    return;
  }
  if (!readSerialBlocking(dataBuffer, TMF8X2X_MASK_PAYLOAD_BYTES, 500)) {
    PRINT_CONST_STR(F("#Err U timeout payload"));
    PRINT_LN();
    return;
  }
  // Must be stopped before reconfiguring.
  if (stateTmf8828 == TMF8828_STATE_MEASURE) {
    tmf8828StopMeasurement(&(tmf8828[0]));
    tmf8828DisableInterrupts(&(tmf8828[0]), 0xFF);
    stateTmf8828 = TMF8828_STATE_STOPPED;
  }
  if (stateTmf8828 != TMF8828_STATE_STOPPED) {
    PRINT_CONST_STR(F("#Err U not stopped"));
    PRINT_LN();
    return;
  }
  // Custom masks are TMF8821 (legacy) only.
  if (modeIsTmf8828) {
    modeIsTmf8828 = 0;
    setMode();
  }
  // Step 1: set spad_map_id=14 (or 15) on the common page first. The device
  // rejects writes to the SPAD-1 page (cid_rid=0x17) with
  // STAT_WARNING_CONFIG_SPAD_1_NOT_ACCEPTED (0x0A) if a pre-defined SPAD map
  // is still selected. Per AN001015 the host must opt into user-defined mode
  // by writing spad_map_id=14/15 to the common page first.
  configSpadId[modeIsTmf8828][configNr] = map_id;
  configure();
  // Step 2: load the SPAD-1 page (cmd 0x17), write the 109-byte mask, commit.
  uint8_t loadCmd = TMF8X2X_CMD_LOAD_PAGE_SPAD_1;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &loadCmd);
  uint8_t status = 0xFF;
  unsigned long t0 = millis();
  while (millis() - t0 < 50) {
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
             TMF8X2X_CMD_STAT_REG, 1, &status);
    if (status == 0x00) break;
  }
  if (status != 0x00) {
    PRINT_CONST_STR(F("#Err U load page status="));
    PRINT_INT(status);
    PRINT_LN();
    return;
  }
  uint8_t loaded = 0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CONFIG_RESULT_REG, 1, &loaded);
  if (loaded != TMF8X2X_CMD_LOAD_PAGE_SPAD_1) {
    PRINT_CONST_STR(F("#Err U wrong page loaded="));
    PRINT_INT(loaded);
    PRINT_LN();
    return;
  }
  uint8_t *p = dataBuffer;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_ENABLE_REG, 30, p);
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_TDC_REG, 72, p + 30);
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_TDCSEL_REG, 3, p + 102);
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_TRAILER_REG, 4, p + 105);
  int8_t cstat = tmf8828WriteConfigPage(&(tmf8828[0]));
  if (cstat != APP_SUCCESS_OK) {
    PRINT_CONST_STR(F("#Err U spad commit="));
    PRINT_INT(cstat);
    uint8_t actual = 0xFF;
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
             TMF8X2X_CMD_STAT_REG, 1, &actual);
    PRINT_CONST_STR(F(" CMD_STAT=0x"));
    PRINT_UINT_HEX(actual);
    // Status registers may show why (DS000693 §8 STAT_WARNING_CONFIG_SPAD_1_N).
    uint8_t st4=0, st5=0, st6=0, st7=0;
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x04, 1, &st4);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x05, 1, &st5);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x06, 1, &st6);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x07, 1, &st7);
    PRINT_CONST_STR(F(" STAT=0x"));
    PRINT_UINT_HEX(st4); PRINT_CHAR(','); PRINT_UINT_HEX(st5); PRINT_CHAR(',');
    PRINT_UINT_HEX(st6); PRINT_CHAR(','); PRINT_UINT_HEX(st7);
    PRINT_LN();
    return;
  }
  PRINT_CONST_STR(F("#U ok map="));
  PRINT_INT(map_id);
  PRINT_CONST_STR(F(" mode=882x"));
  PRINT_LN();
}

// 'V' command: upload the SPAD-2 page (cid_rid=0x18) for sub-capture 1 of
// a time-multiplexed (map_id=15) custom mask. Same 109-byte payload format
// as 'U'. The host should send 'U' (sub-cap 0) first, then 'V' (sub-cap 1),
// then take a measurement. Both sub-cap pages are committed independently;
// the device alternates between them during ranging.
void uploadCustomMaskPage2() {
  if (!readSerialBlocking(dataBuffer, TMF8X2X_MASK_PAYLOAD_BYTES, 500)) {
    PRINT_CONST_STR(F("#Err V timeout payload"));
    PRINT_LN();
    return;
  }
  if (stateTmf8828 == TMF8828_STATE_MEASURE) {
    tmf8828StopMeasurement(&(tmf8828[0]));
    tmf8828DisableInterrupts(&(tmf8828[0]), 0xFF);
    stateTmf8828 = TMF8828_STATE_STOPPED;
  }
  if (stateTmf8828 != TMF8828_STATE_STOPPED) {
    PRINT_CONST_STR(F("#Err V not stopped"));
    PRINT_LN();
    return;
  }
  // Load SPAD-2 page (cid_rid=0x18).
  uint8_t loadCmd = 0x18;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &loadCmd);
  uint8_t status = 0xFF;
  unsigned long t0 = millis();
  while (millis() - t0 < 50) {
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
             TMF8X2X_CMD_STAT_REG, 1, &status);
    if (status == 0x00) break;
  }
  if (status != 0x00) {
    PRINT_CONST_STR(F("#Err V load page status="));
    PRINT_INT(status);
    PRINT_LN();
    return;
  }
  uint8_t loaded = 0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CONFIG_RESULT_REG, 1, &loaded);
  if (loaded != 0x18) {
    PRINT_CONST_STR(F("#Err V wrong page loaded="));
    PRINT_INT(loaded);
    PRINT_LN();
    return;
  }
  uint8_t *p = dataBuffer;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_ENABLE_REG, 30, p);
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_TDC_REG, 72, p + 30);
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_TDCSEL_REG, 3, p + 102);
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_MASK_TRAILER_REG, 4, p + 105);
  int8_t cstat = tmf8828WriteConfigPage(&(tmf8828[0]));
  if (cstat != APP_SUCCESS_OK) {
    PRINT_CONST_STR(F("#Err V spad2 commit="));
    PRINT_INT(cstat);
    PRINT_LN();
    return;
  }
  PRINT_CONST_STR(F("#V ok page=2"));
  PRINT_LN();
}

// 'R' / 'r2' commands: load the SPAD-1 (cid_rid=0x17) or SPAD-2 (cid_rid=0x18)
// config page and dump its 188 register bytes (0x24..0xDF) as CSV. Used by
// the host to read back the firmware-applied SPAD selection for both
// custom-mask uploads and predefined map_ids. For time-multiplexed predefined
// maps, sub-capture 0 lives in SPAD-1 page and sub-capture 1 in SPAD-2 page.
static void dumpSpadPageGeneric(uint8_t loadCmd, uint8_t tag) {
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &loadCmd);
  uint8_t status = 0xFF;
  unsigned long t0 = millis();
  while (millis() - t0 < 50) {
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
             TMF8X2X_CMD_STAT_REG, 1, &status);
    if (status == 0x00) break;
  }
  if (status != 0x00) {
    PRINT_CONST_STR(F("#Err R load status="));
    PRINT_INT(status); PRINT_LN();
    return;
  }
  // 188 bytes is bigger than dataBuffer (192) by margin but it fits.
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CAL_START_REG, TMF8X2X_CAL_PAYLOAD_BYTES, dataBuffer);
  PRINT_CONST_STR(F("#SPAD"));
  PRINT_INT(tag);             // 1 or 2 (which sub-cap page)
  PRINT_CHAR(',');
  for (int i = 0; i < TMF8X2X_CAL_PAYLOAD_BYTES; i++) {
    PRINT_INT(dataBuffer[i]);
    PRINT_CHAR(',');
  }
  PRINT_LN();
}

static void dumpSpadPage() {
  dumpSpadPageGeneric(TMF8X2X_CMD_LOAD_PAGE_SPAD_1, 1);
}

static void dumpSpadPage2() {
  // SPAD-2 page = cid_rid=0x18, the time-multiplexed second sub-capture.
  dumpSpadPageGeneric(0x18, 2);
}

// 'Q' command: run factory calibration against whatever SPAD mask is
// currently active, then read back the 188-byte cal payload and emit it
// over serial. Per DS000693 §7.3, cal must be run with kIter=4000 in a
// dark housing (no target within 40 cm). The device cycle is ≈ 4-10 s
// depending on iterations; we poll CMD_STAT manually for up to 15 s.
// Poll CMD_STAT until command completion. Handles the race where the I2C
// write of a new command hasn't yet propagated when we issue the first read
// (CMD_STAT would still show STAT_OK from the previous command, making poll
// return immediately). Caller passes the command code just written so we
// can confirm we've seen the device acknowledge the new command before
// looking for terminal status.
static int8_t pollCmdStatAfter(uint8_t cmd_just_written, unsigned long timeout_ms) {
  uint8_t s = 0xFF;
  bool saw_cmd_echo = false;
  unsigned long t0 = millis();
  while (millis() - t0 < timeout_ms) {
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
             TMF8X2X_CMD_STAT_REG, 1, &s);
    // Phase 1: wait until the device echoes the new command or reports
    // STAT_ACCEPTED (long-running cmds). Until then, the old STAT_OK is
    // not the answer we want.
    if (!saw_cmd_echo) {
      if (s == cmd_just_written || s == 0x01 /* STAT_ACCEPTED */ ||
          (s >= 0x10 && s != 0xFF)) {
        saw_cmd_echo = true;
      }
      delayInMicroseconds(500);
      continue;
    }
    // Phase 2: command in flight. Wait for terminal status.
    if (s == 0x00) return APP_SUCCESS_OK;
    if (s >= 0x02 && s < 0x10) return -((int8_t)s);
    delayInMicroseconds(2000);
  }
  return APP_ERROR_TIMEOUT;
}

// Backward-compat wrapper for paths that didn't track the command code.
static int8_t pollCmdStat(unsigned long timeout_ms) {
  uint8_t s = 0xFF;
  unsigned long t0 = millis();
  while (millis() - t0 < timeout_ms) {
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
             TMF8X2X_CMD_STAT_REG, 1, &s);
    if (s == 0x00) return APP_SUCCESS_OK;
    if (s >= 0x02 && s < 0x10) return -((int8_t)s);
    delayInMicroseconds(2000);
  }
  return APP_ERROR_TIMEOUT;
}

static void runAndDumpCalibration() {
  if (stateTmf8828 != TMF8828_STATE_STOPPED) {
    PRINT_CONST_STR(F("#Err Q not stopped")); PRINT_LN();
    return;
  }
  uint16_t saved_kiter = configKiloIter[modeIsTmf8828][configNr];
  configKiloIter[modeIsTmf8828][configNr] = 4000;
  configure();

  // Trigger cal. AMS C driver's tmf8828FactoryCalibration has a 2s timeout
  // that is too short for some configs; we poll for up to 15s manually.
  uint8_t cmd = TMF8X2X_CMD_FACTORY_CALIBRATION;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &cmd);
  int8_t stat = pollCmdStatAfter(cmd, 15000);
  if (stat != APP_SUCCESS_OK) {
    PRINT_CONST_STR(F("#Err Q cal status=")); PRINT_INT(stat); PRINT_LN();
    configKiloIter[modeIsTmf8828][configNr] = saved_kiter;
    configure();
    return;
  }
  uint8_t cal_status = 0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x07, 1, &cal_status);

  // Diagnostic: dump CMD_STAT + CONFIG_RESULT right after cal returns OK.
  uint8_t dbg_cs=0, dbg_cr=0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, TMF8X2X_CMD_STAT_REG, 1, &dbg_cs);
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, TMF8X2X_CONFIG_RESULT_REG, 1, &dbg_cr);
  PRINT_CONST_STR(F("#DBG post-cal CMD_STAT=0x")); PRINT_UINT_HEX(dbg_cs);
  PRINT_CONST_STR(F(" CONFIG_RESULT=0x")); PRINT_UINT_HEX(dbg_cr); PRINT_LN();

  // Issue STOP (cmd 0xFF) to ensure clean state before next page-load.
  cmd = 0xFF;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &cmd);
  pollCmdStatAfter(cmd, 200);
  delayInMicroseconds(20000);

  // Now load the factory-cal page; pollCmdStatAfter handles the
  // I2C write-propagation race so the first read doesn't see prior STAT_OK.
  cmd = TMF8X2X_CMD_LOAD_PAGE_FAC_CALIB;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &cmd);
  stat = pollCmdStatAfter(cmd, 5000);
  if (stat != APP_SUCCESS_OK) {
    // Dump the diagnostic register triple before bailing.
    uint8_t cs=0, st4=0, st5=0, st6=0, st7=0, cr=0;
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, TMF8X2X_CMD_STAT_REG, 1, &cs);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x04, 1, &st4);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x05, 1, &st5);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x06, 1, &st6);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x07, 1, &st7);
    i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x20, 1, &cr);
    PRINT_CONST_STR(F("#Err Q load stat=")); PRINT_INT(stat);
    PRINT_CONST_STR(F(" CMD_STAT=0x")); PRINT_UINT_HEX(cs);
    PRINT_CONST_STR(F(" APP_STAT=")); PRINT_INT(st4);
    PRINT_CONST_STR(F(",")); PRINT_INT(st5);
    PRINT_CONST_STR(F(",")); PRINT_INT(st6);
    PRINT_CONST_STR(F(",")); PRINT_INT(st7);
    PRINT_CONST_STR(F(" CONFIG_RESULT=0x")); PRINT_UINT_HEX(cr);
    PRINT_LN();
    configKiloIter[modeIsTmf8828][configNr] = saved_kiter;
    configure();
    return;
  }
  // Verify the right page is loaded.
  uint8_t loaded = 0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CONFIG_RESULT_REG, 1, &loaded);
  if (loaded != TMF8X2X_CMD_LOAD_PAGE_FAC_CALIB) {
    PRINT_CONST_STR(F("#Err Q wrong page loaded=0x"));
    PRINT_UINT_HEX(loaded); PRINT_LN();
    configKiloIter[modeIsTmf8828][configNr] = saved_kiter;
    configure();
    return;
  }
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CAL_START_REG, TMF8X2X_CAL_PAYLOAD_BYTES, dataBuffer);
  PRINT_CONST_STR(F("#CAL_STATUS=")); PRINT_INT(cal_status); PRINT_LN();
  PRINT_CONST_STR(F("#CAL,"));
  for (int i = 0; i < TMF8X2X_CAL_PAYLOAD_BYTES; i++) {
    PRINT_INT(dataBuffer[i]); PRINT_CHAR(',');
  }
  PRINT_LN();
  configKiloIter[modeIsTmf8828][configNr] = saved_kiter;
  configure();
  PRINT_CONST_STR(F("#Q ok")); PRINT_LN();
}

// 'L' command: 'L' + 188 bytes binary = load a previously-saved factory
// calibration page into the device's current mask. We mirror the AMS Python
// driver's setFactoryCalibrationData: write 4-byte page header
// (CID=0x19, TID=0, SIZE_LSB=0xBC, SIZE_MSB=0) followed by the 188 cal data
// bytes starting at register 0x20, in one shot, then commit via cmd 0x15.
// No prior load_cal_page — that path leaves the device in a state where the
// subsequent measurement returns all zeros (observed empirically).
static void loadCalibrationFromHost() {
  // Read the cal payload into dataBuffer offset 4 (leaves 4-byte header room).
  if (!readSerialBlocking(dataBuffer + 4, TMF8X2X_CAL_PAYLOAD_BYTES, 500)) {
    PRINT_CONST_STR(F("#Err L timeout payload")); PRINT_LN();
    return;
  }
  // Page header bytes the device expects on commit (matches what
  // load_cal_page produces in the I2C-visible RAM).
  dataBuffer[0] = TMF8X2X_CMD_LOAD_PAGE_FAC_CALIB;   // 0x19 — CID
  dataBuffer[1] = 0x00;                              // TID
  dataBuffer[2] = TMF8X2X_CAL_PAYLOAD_BYTES;         // SIZE_LSB = 0xBC
  dataBuffer[3] = 0x00;                              // SIZE_MSB
  // Diagnostics before any state change.
  uint8_t cs_before=0, cal_status_before=0, cr_before=0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, TMF8X2X_CMD_STAT_REG, 1, &cs_before);
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x07, 1, &cal_status_before);
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, TMF8X2X_CONFIG_RESULT_REG, 1, &cr_before);
  PRINT_CONST_STR(F("#L pre  CMD_STAT=0x")); PRINT_UINT_HEX(cs_before);
  PRINT_CONST_STR(F(" cal_st=0x")); PRINT_UINT_HEX(cal_status_before);
  PRINT_CONST_STR(F(" CONFIG_RESULT=0x")); PRINT_UINT_HEX(cr_before); PRINT_LN();

  // AMS Python's setFactoryCalibrationData does NOT load_cal_page first —
  // it writes the page header + cal data to registers 0x20..0xDF in one shot,
  // then commits via cmd 0x15. Replicate exactly.
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CONFIG_RESULT_REG, 4 + TMF8X2X_CAL_PAYLOAD_BYTES,
           dataBuffer);
  uint8_t cmd = TMF8X2X_CMD_WRITE_PAGE;
  i2cTxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress,
           TMF8X2X_CMD_STAT_REG, 1, &cmd);
  int8_t stat = pollCmdStatAfter(cmd, 1000);
  if (stat != APP_SUCCESS_OK) {
    PRINT_CONST_STR(F("#Err L commit status=")); PRINT_INT(stat); PRINT_LN();
    return;
  }
  uint8_t cal_status_after = 0, cs_after=0;
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, 0x07, 1, &cal_status_after);
  i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, TMF8X2X_CMD_STAT_REG, 1, &cs_after);
  PRINT_CONST_STR(F("#L ok cal_st=0x"));
  PRINT_UINT_HEX(cal_status_after);
  PRINT_CONST_STR(F(" CMD_STAT=0x"));
  PRINT_UINT_HEX(cs_after); PRINT_LN();
}

// Switch I2C address.
void changeI2CAddress() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    uint8_t newAddr = tmf8828[0].i2cSlaveAddress;
    if (newAddr == TMF8828_SLAVE_ADDR) {
      newAddr = TMF8828_SLAVE_ADDR + 1; // use next i2c slave address
    } else {
      newAddr = TMF8828_SLAVE_ADDR; // back to original
    }
    if (tmf8828ChangeI2CAddress(&(tmf8828[0]), newAddr) != APP_SUCCESS_OK) {
      PRINT_CONST_STR(F("#Err"));
      PRINT_CHAR(SEPARATOR);
    }
  }
  PRINT_CONST_STR(F("I2C Addr="));
  PRINT_INT(tmf8828[0].i2cSlaveAddress);
  PRINT_LN();
}

// enable/disable clock correction
void clockCorrection() {
  clkCorrectionOn = !clkCorrectionOn; // toggle clock correction on/off
  tmf8828ClkCorrection(&(tmf8828[0]), clkCorrectionOn);
  PRINT_CONST_STR(F("Clk corr is "));
  PRINT_INT(clkCorrectionOn);
  PRINT_LN();
}

// wrap through the available configurations and configure the device
// accordingly.
void configure() {
  if (tmf8828Configure(&(tmf8828[0]), configPeriod[modeIsTmf8828][configNr],
                       configKiloIter[modeIsTmf8828][configNr],
                       configSpadId[modeIsTmf8828][configNr],
                       configLowThreshold, configHighThreshold,
                       configPersistance[persistenceNr], configInterruptMask,
                       dumpHistogramOn) == APP_SUCCESS_OK) {
    PRINT_CONST_STR(F("#Conf"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("Period="));
    PRINT_INT(configPeriod[modeIsTmf8828][configNr]);
    PRINT_CONST_STR(F("ms"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("KIter="));
    PRINT_INT(configKiloIter[modeIsTmf8828][configNr]);
    PRINT_CONST_STR(F(" SPAD="));
    PRINT_INT(configSpadId[modeIsTmf8828][configNr]);
    PRINT_CONST_STR(F(" Pers="));
    PRINT_INT(configPersistance[persistenceNr]);
  } else {
    PRINT_CONST_STR(F("#Err"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("Config"));
  }
  PRINT_LN();
}

// enable device and download firmware
void enable(uint32_t imageStartAddress, const unsigned char *image,
            int32_t imageSizeInBytes) {
  if (stateTmf8828 == TMF8828_STATE_DISABLED) {
    tmf8828Enable(&(tmf8828[0]));
    delayInMicroseconds(ENABLE_TIME_MS * 1000);
    tmf8828ClkCorrection(&(tmf8828[0]), clkCorrectionOn);
    tmf8828SetLogLevel(&(tmf8828[0]), logLevels[logLevelIdx]);
    tmf8828Wakeup(&(tmf8828[0]));
    if (tmf8828IsCpuReady(&(tmf8828[0]), CPU_READY_TIME_MS)) {
      if (tmf8828DownloadFirmware(&(tmf8828[0]), imageStartAddress, image,
                                  imageSizeInBytes) == BL_SUCCESS_OK) {
        PRINT_CONST_STR(F(" DWNL"));
        PRINT_LN();
        resetAppState();
        setMode();
        configure();
        stateTmf8828 = TMF8828_STATE_STOPPED;
        printHelp(); // prints on UART usage and waits for user input on serial
        tmf8828ReadDeviceInfo(&(tmf8828[0]));
        printDeviceInfo();
      } else {
        stateTmf8828 = TMF8828_STATE_ERROR;
      }
    } else {
      stateTmf8828 = TMF8828_STATE_ERROR;
    }
  } // else device is already enabled
  else {
    tmf8828ReadDeviceInfo(&(tmf8828[0]));
    printDeviceInfo();
  }
}

// execute factory calibration in state stopped only
void factoryCalibration() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    PRINT_CONST_STR(F("Fact Cal"));
    PRINT_LN();
    tmf8828Configure(&(tmf8828[0]), 1, 4000,
                     configSpadId[modeIsTmf8828][configNr], 0, 0xffff, 0,
                     0x3ffff,
                     0); // no histogram dumping in factory calibration allowed,
                         // 4M iterations for factory calibration recommended
    if (modeIsTmf8828) {
      tmf8828ResetFactoryCalibration(&(tmf8828[0]));
      // there will be 4 factory calibration sets for tmf8828
      int8_t status;
      for (int8_t i = 0; i < 4; i++) {
        status = tmf8828FactoryCalibration(&(tmf8828[0]));
        if (APP_SUCCESS_OK != status)
          break;
      }
      if (APP_SUCCESS_OK == status) {
        configure();
        return;
      }
    } else {
      if (APP_SUCCESS_OK == tmf8828FactoryCalibration(&(tmf8828[0]))) {
        configure();
        return;
      }
    }
    PRINT_CONST_STR(F("#Err"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("fact calib"));
    PRINT_LN();
  }
}

// configure histogram dumping (next dumping bit-mask)
void histogramDumping() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    dumpHistogramOn =
        dumpHistogramOn +
        1; // select histogram dump on/off, and type of histogram dumping
    if (dumpHistogramOn >
        (TMF8828_COM_HIST_DUMP__histogram__electrical_calibration_24_bit_histogram +
         TMF8828_COM_HIST_DUMP__histogram__raw_24_bit_histogram)) {
      dumpHistogramOn = 0; // is off again
    }
    configure();
    PRINT_CONST_STR(F("Histogram is "));
    PRINT_INT(dumpHistogramOn);
    PRINT_LN();
  }
}

static const uint8_t *getPrecollectedFactoryCalibration(uint8_t id) {
  const uint8_t *factory_calib;
  if (modeIsTmf8828) // tmf8828 has only 1 SPAD map, but needs 4 sets of
                     // calibraitond data for this 1 spad map
  {
    if (isLongRangeMode) {
      factory_calib = tmf8828_calib_long_0;
      if (id == 1) {
        factory_calib = tmf8828_calib_long_1;
      } else if (id == 2) {
        factory_calib = tmf8828_calib_long_2;
      } else if (id == 3) {
        factory_calib = tmf8828_calib_long_3;
      }
    }
    else {
      factory_calib = tmf8828_calib_short_0;
      if (id == 1) {
        factory_calib = tmf8828_calib_short_1;
      } else if (id == 2) {
        factory_calib = tmf8828_calib_short_2;
      } else if (id == 3) {
        factory_calib = tmf8828_calib_short_3;
      }
    }
  } else // tmf882x can have different SPAD maps, so need different calibration
         // sets
  {
    if (isLongRangeMode) {
      factory_calib = tmf882x_calib_long_0;
      if (configNr == 1)
        factory_calib = tmf882x_calib_long_1;
    }
    else {
      factory_calib = tmf882x_calib_short_0;
      if (configNr == 1)
        factory_calib = tmf882x_calib_short_1;
    }
  }
  return factory_calib;
}

// load factory calibration page to I2C registers 0x20...
void loadFactoryCalibration() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    if (modeIsTmf8828) // tmf8828 has 4 calibration pages
    {
      tmf8828ResetFactoryCalibration(&(tmf8828[0]));
      tmf8828LoadConfigPageFactoryCalib(&(tmf8828[0]));
      printRegisters(0x20, 0xE0 - 0x20, ',', 0);
      tmf8828WriteConfigPage(&(tmf8828[0])); // advance to next calib page
      tmf8828LoadConfigPageFactoryCalib(&(tmf8828[0]));
      printRegisters(0x20, 0xE0 - 0x20, ',', 1);
      tmf8828WriteConfigPage(&(tmf8828[0])); // advance to next calib page
      tmf8828LoadConfigPageFactoryCalib(&(tmf8828[0]));
      printRegisters(0x20, 0xE0 - 0x20, ',', 2);
      tmf8828WriteConfigPage(&(tmf8828[0])); // advance to next calib page
      tmf8828LoadConfigPageFactoryCalib(&(tmf8828[0]));
      printRegisters(0x20, 0xE0 - 0x20, ',', 3);
      tmf8828WriteConfigPage(&(tmf8828[0])); // advance to next calib page
    } else {
      tmf8828LoadConfigPageFactoryCalib(&(tmf8828[0]));
      printRegisters(0x20, 0xE0 - 0x20, ',', configNr);
    }
  }
}

// decrease logging level
void logLevelDec() {
  if (logLevelIdx > 0) {
    logLevelIdx--;
    tmf8828SetLogLevel(&(tmf8828[0]), logLevels[logLevelIdx]);
  }
  PRINT_CONST_STR(F("Log="));
  PRINT_INT(logLevels[logLevelIdx]);
  PRINT_LN();
}

// increase logging level
void logLevelInc() {
  if (logLevelIdx < NR_LOG_LEVELS - 1) {
    logLevelIdx++;
    tmf8828SetLogLevel(&(tmf8828[0]), logLevels[logLevelIdx]);
  }
  PRINT_CONST_STR(F("Log="));
  PRINT_INT(logLevels[logLevelIdx]);
  PRINT_LN();
}

// start measurement
void measure() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    tmf8828ClrAndEnableInterrupts(&(tmf8828[0]),
                                  TMF8828_APP_I2C_RESULT_IRQ_MASK |
                                      TMF8828_APP_I2C_RAW_HISTOGRAM_IRQ_MASK);
    tmf8828StartMeasurement(&(tmf8828[0]));
    stateTmf8828 = TMF8828_STATE_MEASURE;
  }
}

// select the next configuration and configure
void nextConfiguration() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    configNr = configNr + 1;
    if (configNr > 2) {
      configNr = 0; // wrap around
    }
    configure();
  }
}

// power down by setting PON=0
void powerDown() {
  if (stateTmf8828 == TMF8828_STATE_MEASURE) // stop a measurement first
  {
    tmf8828StopMeasurement(&(tmf8828[0]));
    tmf8828DisableInterrupts(&(tmf8828[0]), 0xFF); // just disable all
    stateTmf8828 = TMF8828_STATE_STOPPED;
  }
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    tmf8828Standby(&(tmf8828[0]));
    stateTmf8828 = TMF8828_STATE_STANDBY;
  }
}

// perform a hardware + software reset
void reset() {
  if (stateTmf8828 != TMF8828_STATE_DISABLED) {
    tmf8828Reset(&(tmf8828[0]));
    PRINT_CONST_STR(F("Reset TMF8828"));
    PRINT_LN();
    stateTmf8828 = TMF8828_STATE_STOPPED;
    setMode();
  }
}

// restore factory calibration for file tmf8828_calib.c
void restoreFactoryCalibration() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    if (modeIsTmf8828) {
      if (APP_SUCCESS_OK ==
              tmf8828ResetFactoryCalibration(
                  &(tmf8828[0])) // First reset, then load all 4 calib pages
          && APP_SUCCESS_OK ==
                 tmf8828SetStoredFactoryCalibration(
                     &(tmf8828[0]), getPrecollectedFactoryCalibration(0)) &&
          APP_SUCCESS_OK ==
              tmf8828SetStoredFactoryCalibration(
                  &(tmf8828[0]), getPrecollectedFactoryCalibration(1)) &&
          APP_SUCCESS_OK ==
              tmf8828SetStoredFactoryCalibration(
                  &(tmf8828[0]), getPrecollectedFactoryCalibration(2)) &&
          APP_SUCCESS_OK ==
              tmf8828SetStoredFactoryCalibration(
                  &(tmf8828[0]), getPrecollectedFactoryCalibration(3))) {
        PRINT_CONST_STR(F("Set fact cal"));
        PRINT_LN();
        return;
      }
    } else if (APP_SUCCESS_OK ==
               tmf8828SetStoredFactoryCalibration(
                   &(tmf8828[0]),
                   getPrecollectedFactoryCalibration(configNr))) {
      PRINT_CONST_STR(F("Set fact cal"));
      PRINT_LN();
      return;
    }
    PRINT_CONST_STR(F("#Err"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("loadCal"));
    PRINT_LN();
  }
}

// set mode to tmf8828 or tmf882x
void setMode() {
  int8_t res;
  if (modeIsTmf8828) {
    res = tmf8828SwitchTo8x8Mode(&(tmf8828[0]));
  } else {
    res = tmf8828SwitchToLegacyMode(&(tmf8828[0]));
  }
  if (APP_SUCCESS_OK != res) {
    PRINT_CONST_STR(F("#Err"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("mode switch to"));
    PRINT_CHAR(SEPARATOR);
    PRINT_INT(modeIsTmf8828);
    PRINT_LN();
    modeIsTmf8828 = 0; // force back to tmf882x mode
  }
}

// set the active ranging mode
void setAccuracyMode() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    if (isLongRangeMode)
      tmf8828SetLongRangeAccuracy(&(tmf8828[0]));
    else
      tmf8828SetShortRangeAccuracy(&(tmf8828[0]));
    configure();
  }
}

// execute a stop
void stop() {
  if (stateTmf8828 == TMF8828_STATE_MEASURE ||
      stateTmf8828 == TMF8828_STATE_STOPPED) {
    tmf8828StopMeasurement(&(tmf8828[0]));
    tmf8828DisableInterrupts(&(tmf8828[0]), 0xFF); // just disable all
    stateTmf8828 = TMF8828_STATE_STOPPED;
  }
}

// set the thresholds to the next configuration
void thresholds() {
  if (stateTmf8828 == TMF8828_STATE_STOPPED) {
    persistenceNr = persistenceNr + 1;
    if (persistenceNr > 2) {
      persistenceNr = 0; // wrap around
    }
    configure();
  }
}

// wakeup sequence
void wakeup() {
  if (stateTmf8828 == TMF8828_STATE_STANDBY) {
    tmf8828Wakeup(&(tmf8828[0]));
    if (tmf8828IsCpuReady(&(tmf8828[0]), CPU_READY_TIME_MS)) {
      stateTmf8828 = TMF8828_STATE_STOPPED;
    } else {
      stateTmf8828 = TMF8828_STATE_ERROR;
    }
  }
}

// Print the current state (stateTmf8828) in a readable format
void printState() {
  if (modeIsTmf8828) {
    PRINT_CONST_STR(F("TMF8828"));
  } else {
    PRINT_CONST_STR(F("TMF882x"));
  }
  PRINT_CONST_STR(F(" state="));
  switch (stateTmf8828) {
  case TMF8828_STATE_DISABLED:
    PRINT_CONST_STR(F("disabled"));
    break;
  case TMF8828_STATE_STANDBY:
    PRINT_CONST_STR(F("standby"));
    break;
  case TMF8828_STATE_STOPPED:
    PRINT_CONST_STR(F("stopped"));
    break;
  case TMF8828_STATE_MEASURE:
    PRINT_CONST_STR(F("measure"));
    break;
  case TMF8828_STATE_ERROR:
    PRINT_CONST_STR(F("error"));
    break;
  default:
    PRINT_CONST_STR(F("???"));
    break;
  }
  PRINT_LN();
}

// print registers either as c-struct or plain
void printRegisters(uint8_t regAddr, uint16_t len, char seperator,
                    uint8_t calibId) {
  if (stateTmf8828 != TMF8828_STATE_DISABLED) {
    uint8_t buf[NR_REGS_PER_LINE];
    uint16_t i;
    uint8_t j;
    if (seperator == ',') {
      if (modeIsTmf8828) {
        PRINT_CONST_STR(
            F("const PROGMEM uint8_t tmf8828_calib_")); // different name for
                                                        // tmf8828
      } else {
        PRINT_CONST_STR(
            F("const PROGMEM uint8_t tmf882x_calib_")); // different name for
                                                        // tmf882x
      }
      if (isLongRangeMode) {
        PRINT_CONST_STR(F("long_"));
      } else {
        PRINT_CONST_STR(F("short_"));
      }
      PRINT_INT(calibId);
      PRINT_CONST_STR(F("[] = {"));
      PRINT_LN();
    }
    for (i = 0; i < len;
         i += NR_REGS_PER_LINE) // if len is not a multiple of 8, we will print
                                // a bit more registers ....
    {
      uint8_t *ptr = buf;
      i2cRxReg(&(tmf8828[0]), tmf8828[0].i2cSlaveAddress, regAddr,
               NR_REGS_PER_LINE, buf);
      if (seperator == ' ') {
        PRINT_CONST_STR(F("0x"));
        PRINT_UINT_HEX(regAddr);
        PRINT_CONST_STR(F(": "));
      }
      for (j = 0; j < NR_REGS_PER_LINE; j++) {
        PRINT_CONST_STR(F(" 0x"));
        PRINT_UINT_HEX(*ptr++);
        PRINT_CHAR(seperator);
      }
      PRINT_LN();
      regAddr = regAddr + 8;
    }
    if (seperator == ',') {
      PRINT_CONST_STR(F("};"));
      PRINT_LN();
    }
  }
}

// -------------------------------------------------------------------------------------------------------------

void printDeviceInfo() {
  PRINT_CONST_STR(F("Driver "));
  PRINT_INT(tmf8828[0].info.version[0]);
  PRINT_CHAR('.');
  PRINT_INT(tmf8828[0].info.version[1]);
  PRINT_CONST_STR(F(" FW "));
  PRINT_INT(tmf8828[0].device.appVersion[0]);
  PRINT_CHAR('.');
  PRINT_INT(tmf8828[0].device.appVersion[1]);
  PRINT_CHAR('.');
  PRINT_INT(tmf8828[0].device.appVersion[2]);
  PRINT_CHAR('.');
  PRINT_INT(tmf8828[0].device.appVersion[3]);
  PRINT_CHAR('.');
  PRINT_CONST_STR(F(" Chip "));
  PRINT_INT(tmf8828[0].device.chipVersion[0]);
  PRINT_CHAR('.');
  PRINT_INT(tmf8828[0].device.chipVersion[1]);
  PRINT_CONST_STR(F(" Serial 0x"));
  PRINT_UINT_HEX(tmf8828[0].device.deviceSerialNumber);
  PRINT_LN();
}

// Function prints a help screen
void printHelp() {
  PRINT_CONST_STR(F("TMF8828 Arduino Driver"));
  PRINT_LN();
  PRINT_CONST_STR(F("UART commands"));
  PRINT_LN();
  PRINT_CONST_STR(F("a ... dump registers"));
  PRINT_LN();
  PRINT_CONST_STR(F("c ... next configuration"));
  PRINT_LN();
  PRINT_CONST_STR(F("d ... disable device"));
  PRINT_LN();
  PRINT_CONST_STR(F("e ... enable device and download TMF8828 FW"));
  PRINT_LN();
  PRINT_CONST_STR(F("E ... enable device and download TMF8821 FW"));
  PRINT_LN();
  PRINT_CONST_STR(F("f ... do fact calib"));
  PRINT_LN();
  PRINT_CONST_STR(F("h ... help "));
  PRINT_LN();
  PRINT_CONST_STR(F("i ... i2c addr. change"));
  PRINT_LN();
  PRINT_CONST_STR(F("l ... load fact calib"));
  PRINT_LN();
  PRINT_CONST_STR(F("m ... measure"));
  PRINT_LN();
  PRINT_CONST_STR(F("o ... toggle between TMF8828 and TMF882X"));
  PRINT_LN();
  PRINT_CONST_STR(F("O ... toggle between short range and long range accuracy modes"));
  PRINT_LN();
  PRINT_CONST_STR(F("p ... power down"));
  PRINT_LN();
  PRINT_CONST_STR(F("r ... restore fact calib from file"));
  PRINT_LN();
  PRINT_CONST_STR(F("s ... stop measure"));
  PRINT_LN();
  PRINT_CONST_STR(F("t ... next persistance set"));
  PRINT_LN();
  PRINT_CONST_STR(F("w ... wakeup"));
  PRINT_LN();
  PRINT_CONST_STR(F("x ... clock corr on/off"));
  PRINT_LN();
  PRINT_CONST_STR(F("z ... histogram"));
  PRINT_LN();
  PRINT_CONST_STR(F("U ... upload custom SPAD mask SPAD-1 page: 'U' + map_id byte (14|15) + 109 byte payload"));
  PRINT_LN();
  PRINT_CONST_STR(F("V ... upload SPAD-2 page for time-mux (map_id=15) sub-cap 1: 'V' + 109 byte payload"));
  PRINT_LN();
  PRINT_CONST_STR(F("R ... read SPAD-1 config page (debug; 188 bytes CSV)"));
  PRINT_LN();
  PRINT_CONST_STR(F("X ... read SPAD-2 config page (debug; 188 bytes CSV)"));
  PRINT_LN();
  PRINT_CONST_STR(F("Q ... factory-cal against current mask + dump 188-byte cal page"));
  PRINT_LN();
  PRINT_CONST_STR(F("L ... load previously-saved cal page: 'L' + 188 byte payload"));
  PRINT_LN();
  PRINT_CONST_STR(F("Y ... print current #Conf line (no state change)"));
  PRINT_LN();
  PRINT_CONST_STR(F("+ ... log+"));
  PRINT_LN();
  PRINT_CONST_STR(F("- ... log-"));
  PRINT_LN();
  PRINT_CONST_STR(F("# ... reset"));
  PRINT_LN();
}

// Function checks the UART for received characters and interprets them
int8_t serialInput() {
  char rx;
  int8_t recv;
  do {
    recv = inputGetKey(&rx);
    if (rx < 33 || rx >= 126) // skip all control characters and DEL
    {
      continue; // nothing to do here
    } else {
      if (rx == 'h') {
        printHelp();
      } else if (rx == 'c') // show and use next configuration
      {
        nextConfiguration();
      } else if (rx == 'e') // enable
      {
        enable(tmf8828_image_start, tmf8828_image, tmf8828_image_length);
      } else if (rx == 'E') // enable
      {
        enable(tmf882x_image_start, tmf882x_image, tmf882x_image_length);
      } else if (rx == 'd') // disable
      {
        tmf8828Disable(&(tmf8828[0]));
        stateTmf8828 = TMF8828_STATE_DISABLED;
      } else if (rx == 'w') // wakeup
      {
        wakeup();
      } else if (rx == 'p') // power down
      {
        powerDown();
      } else if (rx == 'o') {
        modeIsTmf8828 = !modeIsTmf8828;
        setMode();
      } else if (rx == 'O') {
        isLongRangeMode = !isLongRangeMode;
        setAccuracyMode();
      } else if (rx == 'm') {
        measure();
      } else if (rx == 's') {
        stop();
      } else if (rx == 'f') {
        factoryCalibration();
      } else if (rx == 'l') {
        loadFactoryCalibration();
      } else if (rx == 'r') {
        restoreFactoryCalibration();
      } else if (rx == 'z') {
        histogramDumping();
      } else if (rx == 'a') {
        if (stateTmf8828 != TMF8828_STATE_DISABLED) {
          printRegisters(0x00, 256, ' ', 0);
        }
      } else if (rx == 'x') {
        clockCorrection();
      } else if (rx == 'i') {
        changeI2CAddress();
      } else if (rx == 't') // show and use next persistanc configuration
      {
        thresholds();
      } else if (rx == '+') // increase logging
      {
        logLevelInc();
      } else if (rx == '-') // decrease logging
      {
        logLevelDec();
      } else if (rx == '#') // reset chip to test the reset function itself
      {
        reset();
      } else if (rx == 'q') // terminate on device where this can be done
      {
        return 0; // terminate if possible
      } else if (rx == 'U') {
        uploadCustomMask();
      } else if (rx == 'V') {
        uploadCustomMaskPage2();
      } else if (rx == 'R') {
        dumpSpadPage();
      } else if (rx == 'X') {
        dumpSpadPage2();          // SPAD-2 page (sub-cap 1 of time-mux maps)
      } else if (rx == 'Q') {
        runAndDumpCalibration();
      } else if (rx == 'L') {
        loadCalibrationFromHost();
      } else if (rx == 'Y') {
        // Print the currently-applied #Conf line for the active mode and
        // config slot. Pure read, no device-side state changes. Used by
        // the host to record the firmware-applied values into capture
        // metadata after configure() commands.
        PRINT_CONST_STR(F("#Conf"));
        PRINT_CHAR(SEPARATOR);
        PRINT_CONST_STR(F("Period="));
        PRINT_INT(configPeriod[modeIsTmf8828][configNr]);
        PRINT_CONST_STR(F("ms"));
        PRINT_CHAR(SEPARATOR);
        PRINT_CONST_STR(F("KIter="));
        PRINT_INT(configKiloIter[modeIsTmf8828][configNr]);
        PRINT_CONST_STR(F(" SPAD="));
        PRINT_INT(configSpadId[modeIsTmf8828][configNr]);
        PRINT_CONST_STR(F(" Pers="));
        PRINT_INT(configPersistance[persistenceNr]);
        PRINT_LN();
      } else if (rx == 'I' || rx == 'M' || rx == 'P') {
        // Multi-char param: 'I<dec>\n' iter, 'M<dec>\n' map id, 'P<dec>\n' period.
        char which = rx;
        uint32_t v = 0;
        char c;
        unsigned long deadline = millis() + 200;
        while (millis() < deadline) {
          if (inputGetKey(&c)) {
            if (c == '\n' || c == '\r') break;
            if (c >= '0' && c <= '9') {
              v = v * 10 + (uint32_t)(c - '0');
              deadline = millis() + 200;
            }
          }
        }
        if (which == 'I' && v > 0 && v <= 0xFFFF) {
          configKiloIter[modeIsTmf8828][configNr] = (uint16_t)v;
        } else if (which == 'M' && v >= 1 && v <= 15) {
          configSpadId[modeIsTmf8828][configNr] = (uint8_t)v;
        } else if (which == 'P' && v <= 0xFFFF) {
          configPeriod[modeIsTmf8828][configNr] = (uint16_t)v;
        }
        if (stateTmf8828 == TMF8828_STATE_STOPPED) {
          configure();
        }
        PRINT_CONST_STR(F("#Set "));
        PRINT_CHAR(which);
        PRINT_CHAR('=');
        PRINT_INT(v);
        PRINT_LN();
      } else {
        PRINT_CONST_STR(F("#Err"));
        PRINT_CHAR(SEPARATOR);
        PRINT_CONST_STR(F("Cmd "));
        PRINT_CHAR(rx);
        PRINT_LN();
      }
    }
    printState();
  } while (recv);
  return 1;
}

// set target to defined configuration after enabling, needed by demo GUI
void resetAppState() {
  stateTmf8828 = TMF8828_STATE_DISABLED;
  configNr = 0; // rotate through the given configurations
  persistenceNr = 0;
  clkCorrectionOn = 1;
  dumpHistogramOn = 0; // default is off
  irqTriggered = 0;
  modeIsTmf8828 = 1; // default is tmf8828
  isLongRangeMode = 1; // default is long range mode
}

// interrupt handler is called when INT pin goes low
void interruptHandler(void) { irqTriggered = 1; }

// -------------------------------------------------------------------------------------------------------------

// Arduino setup function is only called once at startup. Do all the HW
// initialisation stuff here.
void setupFn(uint8_t logLevelIdx, uint32_t baudrate,
             uint32_t i2cClockSpeedInHz) {
  logLevel = logLevelIdx;

  configurePins(&(tmf8828[0]));

  // start serial and i2c
  inputOpen(baudrate);
  i2cOpen(&(tmf8828[0]), i2cClockSpeedInHz);

  resetAppState();
  tmf8828Initialise(&(tmf8828[0]));
  tmf8828SetLogLevel(&(tmf8828[0]), logLevels[logLevelIdx]);
  setInterruptHandler(interruptHandler);
  tmf8828Disable(&(tmf8828[0])); // this resets the I2C address in the device
  delayInMicroseconds(CAP_DISCHARGE_TIME_MS *
                      1000); // wait for a proper discharge of the cap
  printHelp();
}

// Arduino main loop function, is executed cyclic
int8_t loopFn() {
  int8_t res = APP_SUCCESS_OK;
  uint8_t intStatus = 0;
  int8_t exit = serialInput(); // handle any keystrokes from UART

#if (defined(USE_INTERRUPT_TO_TRIGGER_READ) &&                                 \
     (USE_INTERRUPT_TO_TRIGGER_READ != 0))
  if (irqTriggered)
#else
  if (/*stateTmf8828 == TMF8828_STATE_STOPPED ||*/ stateTmf8828 ==
      TMF8828_STATE_MEASURE)
#endif
  {
    disableInterrupts();
    irqTriggered = 0;
    enableInterrupts();
    intStatus = tmf8828GetAndClrInterrupts(
        &(tmf8828[0]),
        TMF8828_APP_I2C_RESULT_IRQ_MASK | TMF8828_APP_I2C_ANY_IRQ_MASK |
            TMF8828_APP_I2C_RAW_HISTOGRAM_IRQ_MASK); // always clear also the
                                                     // ANY interrupt
    if (intStatus &
        TMF8828_APP_I2C_RESULT_IRQ_MASK) // check if a result is available
                                         // (ignore here the any interrupt)
    {
      res = tmf8828ReadResults(&(tmf8828[0]));
    }
    if (intStatus & TMF8828_APP_I2C_RAW_HISTOGRAM_IRQ_MASK) {
      res =
          tmf8828ReadHistogram(&(tmf8828[0])); // read a (partial) raw histogram
    }
  }

  if (res !=
      APP_SUCCESS_OK) // in case that fails there is some error in programming
                      // or on the device, this should not happen
  {
    tmf8828StopMeasurement(&(tmf8828[0]));
    tmf8828DisableInterrupts(&(tmf8828[0]), 0xFF);
    stateTmf8828 = TMF8828_STATE_STOPPED;
    PRINT_CONST_STR(F("#Err"));
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("inter"));
    PRINT_CHAR(SEPARATOR);
    PRINT_INT(intStatus);
    PRINT_CHAR(SEPARATOR);
    PRINT_CONST_STR(F("but no data"));
    PRINT_LN();
  }
  return exit;
}

// Arduino has no terminate function but PC has.
void terminateFn() {
  tmf8828Disable(&(tmf8828[0]));
  clrInterruptHandler();

  i2cClose(&(tmf8828[0]));
  inputClose();
}
