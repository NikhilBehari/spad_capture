/*
 * platform.cpp  --  Arduino (Wire) implementation of the VL53LMZ ULD I2C layer
 * ----------------------------------------------------------------------------
 * Implements the six primitives declared in platform.h on top of Arduino
 * `Wire`. The VL53LMZ uses 16-bit register addresses (big-endian on the wire)
 * and transfers can be far larger than the Arduino I2C buffer (typically 32
 * bytes), so multi-byte reads/writes are chunked.
 *
 * `p_platform->address` is the 8-bit address (0x52); Wire wants the 7-bit
 * address (0x29 == 0x52 >> 1).
 * ----------------------------------------------------------------------------
 */

#include <Arduino.h>
#include <Wire.h>

#include "platform.h"

/* Conservative per-transaction payload chunk. The Arduino core's Wire buffer
 * is commonly 32 bytes; reserve 2 for the register address on writes. */
static const uint16_t I2C_CHUNK = 30;

static inline uint8_t addr7(VL53LMZ_Platform *p) {
  return (uint8_t)(p->address >> 1);
}

extern "C" uint8_t RdByte(VL53LMZ_Platform *p_platform,
                          uint16_t RegisterAddress, uint8_t *p_value) {
  return RdMulti(p_platform, RegisterAddress, p_value, 1);
}

extern "C" uint8_t WrByte(VL53LMZ_Platform *p_platform,
                          uint16_t RegisterAddress, uint8_t value) {
  return WrMulti(p_platform, RegisterAddress, &value, 1);
}

extern "C" uint8_t RdMulti(VL53LMZ_Platform *p_platform,
                           uint16_t RegisterAddress, uint8_t *p_values,
                           uint32_t size) {
  const uint8_t a = addr7(p_platform);
  uint32_t done = 0;

  while (done < size) {
    uint16_t reg = (uint16_t)(RegisterAddress + done);
    uint32_t chunk = size - done;
    if (chunk > I2C_CHUNK) chunk = I2C_CHUNK;

    /* set the read pointer (16-bit register, big-endian), no STOP */
    Wire.beginTransmission(a);
    Wire.write((uint8_t)(reg >> 8));
    Wire.write((uint8_t)(reg & 0xFF));
    if (Wire.endTransmission(false) != 0) return 255;

    uint8_t got = Wire.requestFrom(a, (uint8_t)chunk);
    if (got != chunk) return 255;
    for (uint32_t i = 0; i < chunk; i++) p_values[done + i] = (uint8_t)Wire.read();

    done += chunk;
  }
  return 0;
}

extern "C" uint8_t WrMulti(VL53LMZ_Platform *p_platform,
                           uint16_t RegisterAddress, uint8_t *p_values,
                           uint32_t size) {
  const uint8_t a = addr7(p_platform);
  uint32_t done = 0;

  while (done < size) {
    uint16_t reg = (uint16_t)(RegisterAddress + done);
    uint32_t chunk = size - done;
    if (chunk > I2C_CHUNK) chunk = I2C_CHUNK;

    Wire.beginTransmission(a);
    Wire.write((uint8_t)(reg >> 8));
    Wire.write((uint8_t)(reg & 0xFF));
    for (uint32_t i = 0; i < chunk; i++) Wire.write(p_values[done + i]);
    if (Wire.endTransmission(true) != 0) return 255;

    done += chunk;
  }
  return 0;
}

extern "C" uint8_t WaitMs(VL53LMZ_Platform *p_platform, uint32_t TimeMs) {
  (void)p_platform;
  delay(TimeMs);
  return 0;
}

/* Byte-swap a buffer in 4-byte words (the ULD calls this for endian fixups). */
extern "C" void SwapBuffer(uint8_t *buffer, uint16_t size) {
  for (uint16_t i = 0; i < size; i += 4) {
    uint32_t w = ((uint32_t)buffer[i] << 24) | ((uint32_t)buffer[i + 1] << 16) |
                 ((uint32_t)buffer[i + 2] << 8) | (uint32_t)buffer[i + 3];
    memcpy(&buffer[i], &w, 4);
  }
}
