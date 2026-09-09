/*
 * platform.h  --  Arduino (Wire) porting layer for the ST VL53LMZ ULD
 * ----------------------------------------------------------------------------
 * The VL53LMZ ULD core (vl53lmz_api.c + the CNH plugin) is platform-agnostic:
 * it calls the I2C primitives declared below, which the integrator implements
 * for their MCU. This file declares the VL53LMZ_Platform descriptor and those
 * primitives; platform.cpp implements them on top of Arduino `Wire`.
 *
 * Drop the ULD `inc/`+`src/` next to this file (see firmware/README.md) and
 * the sketch compiles as a self-contained Arduino project.
 * ----------------------------------------------------------------------------
 */

#ifndef VL53L8CH_PLATFORM_H_
#define VL53L8CH_PLATFORM_H_

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/*
 * ULD build-time configuration. The ULD core includes this platform.h (via
 * vl53lmz_api.h) and expects these macros here. They must match spad_center's
 * Platform/platform.h so the ULD result struct layout / RAM footprint matches.
 *
 *   VL53LMZ_NB_TARGET_PER_ZONE : targets per zone (1..4); sizes result buffers.
 *   VL53LMZ_DISABLE_*          : drop unused result outputs to save RAM. We
 *                                stream CNH only, so the standard distance/
 *                                signal/status outputs are not needed.
 */
#define VL53LMZ_NB_TARGET_PER_ZONE 1U

#define VL53LMZ_DISABLE_NB_SPADS_ENABLED
#define VL53LMZ_DISABLE_RANGE_SIGMA_MM
#define VL53LMZ_DISABLE_REFLECTANCE_PERCENT
#define VL53LMZ_DISABLE_TARGET_STATUS
#define VL53LMZ_DISABLE_MOTION_INDICATOR

/*
 * Platform descriptor required by the ULD. For the X-NUCLEO-53L8A1 shield on
 * a single-sensor Arduino setup the only field needed is the I2C address
 * (8-bit form, e.g. VL53LMZ_DEFAULT_I2C_ADDRESS == 0x52).
 */
typedef struct {
  uint16_t address;
} VL53LMZ_Platform;

/* These are the only entry points the ULD core uses to reach the bus. */
uint8_t RdByte(VL53LMZ_Platform *p_platform, uint16_t RegisterAddress, uint8_t *p_value);
uint8_t WrByte(VL53LMZ_Platform *p_platform, uint16_t RegisterAddress, uint8_t value);
uint8_t RdMulti(VL53LMZ_Platform *p_platform, uint16_t RegisterAddress, uint8_t *p_values, uint32_t size);
uint8_t WrMulti(VL53LMZ_Platform *p_platform, uint16_t RegisterAddress, uint8_t *p_values, uint32_t size);
uint8_t WaitMs(VL53LMZ_Platform *p_platform, uint32_t TimeMs);
void    SwapBuffer(uint8_t *buffer, uint16_t size);

#ifdef __cplusplus
}
#endif

#endif /* VL53L8CH_PLATFORM_H_ */
