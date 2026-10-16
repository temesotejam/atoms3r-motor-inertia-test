#pragma once
#include <stdint.h>
#include <math.h>

namespace inertia {
constexpr uint32_t kSampleUs = 10000;
constexpr uint32_t kPreUs = 500000;
constexpr uint32_t kPostUs = 2000000;
constexpr uint32_t kStillUs = 1000000;
constexpr uint32_t kSettleTimeoutUs = 30000000;
constexpr uint32_t kDeadlineToleranceUs = 10000;
constexpr uint32_t kMaxRows = 50000;
constexpr unsigned kMaxPulses = 30;
enum class Phase : uint8_t { Idle, Settling, Pre, Pulse, Post, Done, Stopped, Fault };
inline const char* name(Phase p) {
  switch (p) {
    case Phase::Idle: return "idle"; case Phase::Settling: return "settling";
    case Phase::Pre: return "pre"; case Phase::Pulse: return "pulse";
    case Phase::Post: return "post"; case Phase::Done: return "done";
    case Phase::Stopped: return "stopped"; default: return "fault";
  }
}
inline bool active(Phase p) {
  return p == Phase::Settling || p == Phase::Pre || p == Phase::Pulse || p == Phase::Post;
}
struct Settings {
  bool single = true;
  bool response = false;
  int16_t single_mA = 100;
  int16_t levels_mA[3] = {50, 100, 150};
  uint16_t duration_ms = 100;
  uint8_t repeats = 3;
  uint16_t rpm_limit = 1500;
  uint16_t min_voltage_mV = 6000;
  char label[25] = "A1";
};
inline const char* validate(const Settings& s) {
  if (s.duration_ms < 50 || s.duration_ms > 500) return "pulse_width_50_to_500_ms";
  if (s.repeats < 1 || s.repeats > 5) return "repeats_1_to_5";
  if (s.response && (s.single || s.repeats > 3 || s.single_mA != 300))
    return "response_requires_300mA_and_1_to_3_repeats";
  if (s.rpm_limit < 300 || s.rpm_limit > 2500) return "rpm_limit_300_to_2500";
  if (s.min_voltage_mV < 5000 || s.min_voltage_mV > 12000) return "voltage_limit_5000_to_12000";
  if (s.single || s.response) {
    if (s.single_mA == 0 || s.single_mA < -300 || s.single_mA > 300) return "single_current_1_to_300_mA";
  } else {
    for (unsigned i = 0; i < 3; ++i)
      if (s.levels_mA[i] < 30 || s.levels_mA[i] > 300) return "levels_30_to_300_mA";
    if (!(s.levels_mA[0] < s.levels_mA[1] && s.levels_mA[1] < s.levels_mA[2]))
      return "levels_must_increase";
  }
  return nullptr;
}
inline unsigned pulseCount(const Settings& s) {
  return s.response ? 8 * s.repeats : (s.single ? 1 : 6 * s.repeats);
}
inline uint32_t samplePeriod(const Settings& s) { return s.response ? 5000 : kSampleUs; }
inline uint32_t postPeriod(const Settings& s) { return s.response ? 3000000 : kPostUs; }
inline uint16_t durationMs(const Settings& s, unsigned index) {
  if (!s.response) return s.duration_ms;
  constexpr uint16_t widths[] = {30, 50, 100, 200};
  unsigned level = (index % 8) / 2;
  if ((index / 8) % 2) level = 3 - level;
  return widths[level];
}
inline int64_t positionDelta(int32_t now, int32_t before) {
  const uint32_t d = static_cast<uint32_t>(now) - static_cast<uint32_t>(before);
  return d <= INT32_MAX ? static_cast<int64_t>(d) : static_cast<int64_t>(d) - 4294967296LL;
}
inline int16_t command(const Settings& s, unsigned index) {
  if (s.response) {
    int sign = index % 2 ? -1 : 1;
    if ((index / 8) % 2) sign = -sign;
    return sign * 300;
  }
  if (s.single) return s.single_mA;
  // Alternate signs; reverse level order each repetition to limit drift.
  unsigned level = (index % 6) / 2;
  if ((index / 6) % 2) level = 2 - level;
  int sign = (index % 2) ? -1 : 1;
  if ((index / 6) % 2) sign = -sign;
  return sign * s.levels_mA[level];
}
struct Health {
  bool reads_ok = true;
  float rpm = 0;
  float current_mA = 0;
  uint16_t voltage_mV = 7400;
  int16_t temperature_C = 25;
  uint8_t error = 0;
  uint32_t slow_age_us = 0;
};
inline const char* unhealthy(const Settings& s, const Health& h) {
  if (!h.reads_ok || !isfinite(h.rpm) || !isfinite(h.current_mA)) return "telemetry_read_failed";
  if (h.error) return "motor_error";
  if (h.slow_age_us > 250000) return "motor_status_stale";
  if (fabsf(h.rpm) >= s.rpm_limit) return "rpm_limit";
  if (fabsf(h.current_mA) > 600) return "actual_current_limit";
  if (h.voltage_mV < s.min_voltage_mV) return "voltage_low";
  if (h.temperature_C >= 60) return "temperature_limit";
  return nullptr;
}
// The firmware uses real microsecond timestamps, never nominal sample indices.
inline bool due(uint64_t now, uint64_t deadline) { return deadline && now >= deadline; }
inline bool elapsed(uint64_t now, uint64_t since, uint64_t duration) {
  // A transition can set since from a clock read later than the caller's
  // cached now. Unsigned subtraction alone would treat that as a huge wait.
  return now >= since && now - since >= duration;
}
inline bool stoppedLongEnough(uint64_t now, uint64_t since) { return since && elapsed(now, since, kStillUs); }
} // namespace inertia
