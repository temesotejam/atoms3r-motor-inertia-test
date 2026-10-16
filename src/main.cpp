#include <Arduino.h>
#include <M5Unified.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <esp_timer.h>
#include <esp_heap_caps.h>
#include <atomic>
#include "protocol.h"
#include "web_page.h"

using inertia::Phase;
using inertia::Settings;
constexpr char kRevision[] = "atoms3r_inertia_v1_20261016";
constexpr uint8_t kAddr = 0x64;
WebServer server(80);
portMUX_TYPE snapshotMux = portMUX_INITIALIZER_UNLOCKED;
std::atomic<bool> stopRequested{false}, pending{false};

struct Pulse {
  uint32_t on = 0, zero = 0, off = 0;
  int16_t current = 0;
  bool complete = false;
};
struct Row {
  uint32_t t, speed_start, speed_end, current_start, current_end;
  uint32_t slow_t, i2c_errors, missed, late, work;
  int32_t speed_raw, current_raw;
  uint16_t voltage;
  int16_t temperature, command;
  uint8_t pulse, phase, valid, status, error, output;
};
struct Snapshot {
  Phase phase = Phase::Idle;
  float rpm = NAN, current = NAN;
  uint16_t voltage = 0;
  int16_t temperature = 0;
  uint32_t rows = 0, missed = 0, errors = 0, run = 0;
  unsigned pulse = 0, total = 0;
  bool ready = false, stop_confirmed = false, frozen = false;
  char reason[64] = "initializing";
} snapshot;
enum class RequestType : uint8_t { Start, Clear };
struct Request { RequestType type; Settings settings; };
QueueHandle_t requests;
Row* rows = nullptr;
Pulse pulses[inertia::kMaxPulses];
Settings settings;
Phase phase = Phase::Idle;
inertia::Health health;
uint32_t rowCount = 0, missed = 0, errors = 0, runCounter = 0, bootId = 0;
unsigned pulseIndex = 0;
uint64_t runStart = 0, phaseStart = 0, stillSince = 0, pulseDeadline = 0;
uint64_t nextSample = 0, slowTime = 0, lastStopRetry = 0;
int32_t speedRaw = 0, currentRaw = 0;
uint8_t motorStatus = 0, motorError = 0;
int16_t commandMa = 0;
bool output = false, initialized = false, stopConfirmed = false, logFrozen = true;
const char* reason = "initializing";

uint64_t clockUs() { return esp_timer_get_time(); }
uint32_t relative(uint64_t t) { return runStart && t >= runStart ? static_cast<uint32_t>(t - runStart) : 0; }
Snapshot state() {
  portENTER_CRITICAL(&snapshotMux); Snapshot s = snapshot; portEXIT_CRITICAL(&snapshotMux); return s;
}
void publish() {
  Snapshot s;
  s.phase = phase; s.rpm = health.rpm; s.current = health.current_mA;
  s.voltage = health.voltage_mV; s.temperature = health.temperature_C;
  s.rows = rowCount; s.missed = missed; s.errors = errors; s.run = runCounter;
  s.pulse = runStart ? pulseIndex + 1 : 0; s.total = runStart ? inertia::pulseCount(settings) : 0;
  s.ready = initialized && stopConfirmed && rows; s.stop_confirmed = stopConfirmed;
  s.frozen = logFrozen;
  strlcpy(s.reason, reason, sizeof(s.reason));
  portENTER_CRITICAL(&snapshotMux); snapshot = s; portEXIT_CRITICAL(&snapshotMux);
}
bool write8(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(kAddr); Wire.write(reg); Wire.write(v);
  bool ok = Wire.endTransmission() == 0; if (!ok) ++errors; return ok;
}
bool write32(uint8_t reg, int32_t v) {
  Wire.beginTransmission(kAddr); Wire.write(reg);
  for (int i = 0; i < 4; ++i) Wire.write(static_cast<uint8_t>(static_cast<uint32_t>(v) >> (i * 8)));
  bool ok = Wire.endTransmission() == 0; if (!ok) ++errors; return ok;
}
bool readReg(uint8_t reg, uint8_t* data, unsigned n) {
  Wire.beginTransmission(kAddr); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) { ++errors; return false; }
  if (Wire.requestFrom(kAddr, static_cast<size_t>(n), true) != n) {
    while (Wire.available()) Wire.read(); ++errors; return false;
  }
  for (unsigned i = 0; i < n; ++i) data[i] = Wire.read();
  return true;
}
bool read8(uint8_t reg, uint8_t& v) { return readReg(reg, &v, 1); }
bool read32(uint8_t reg, int32_t& v) {
  uint8_t b[4]; if (!readReg(reg, b, 4)) return false;
  v = static_cast<int32_t>(uint32_t(b[0]) | uint32_t(b[1]) << 8 | uint32_t(b[2]) << 16 | uint32_t(b[3]) << 24);
  return true;
}
// Always attempt both zero-current and output-disable, even if one write fails.
bool motorOff() {
  const bool zero = write32(0xB0, 0);
  const uint32_t zeroAt = relative(clockUs());
  const bool off = write8(0x00, 0);
  const uint32_t offAt = relative(clockUs());
  uint8_t readback = 255;
  const bool verified = read8(0x00, readback) && readback == 0;
  if (runStart && pulseIndex < inertia::kMaxPulses && pulses[pulseIndex].on) {
    if (zero && !pulses[pulseIndex].zero) pulses[pulseIndex].zero = zeroAt;
    if (off && verified && !pulses[pulseIndex].off) pulses[pulseIndex].off = offAt;
  }
  if (zero) commandMa = 0;
  if (off && verified) output = false;
  stopConfirmed = zero && off && verified;
  lastStopRetry = clockUs();
  return stopConfirmed;
}
void finish(Phase p, const char* why) {
  motorOff(); pulseDeadline = 0; phase = p; reason = why;
  if (!stopConfirmed) reason = "stop_unconfirmed_cut_motor_power";
  publish();
}
bool initializeMotor() {
  initialized = false;
  const bool off = motorOff();
  const bool mode = write8(0x01, 3);
  initialized = off && mode;
  return initialized;
}
void serviceTimers() {
  const uint64_t now = clockUs();
  // AtomS3R/AtomS3RCAM front button is GPIO41. Read it here as well as in
  // Arduino loop so a slow HTTP client cannot delay the local STOP action.
  if (digitalRead(41) == LOW && inertia::active(phase)) stopRequested.store(true);
  if (stopRequested.exchange(false)) {
    if (inertia::active(phase)) finish(Phase::Stopped, "operator_stop");
    else motorOff();
  }
  if (phase == Phase::Pulse && inertia::due(now, pulseDeadline)) {
    const uint64_t expectedEnd = pulseDeadline;
    if (!motorOff()) { finish(Phase::Fault, "stop_write_failed"); return; }
    pulseDeadline = 0;
    const uint64_t zeroAt = runStart + pulses[pulseIndex].zero;
    if (zeroAt > expectedEnd + inertia::kDeadlineToleranceUs) {
      finish(Phase::Fault, "pulse_deadline_overrun"); return;
    }
    phase = Phase::Post; phaseStart = clockUs(); reason = "recording_coast";
  }
  if (phase == Phase::Settling) {
    if (now - phaseStart > inertia::kSettleTimeoutUs) {
      finish(Phase::Fault, "wheel_did_not_stop_in_30s"); return;
    }
    if (inertia::stoppedLongEnough(now, stillSince)) {
      phase = Phase::Pre; phaseStart = now; reason = "recording_pre";
    }
  }
  if (phase == Phase::Pre && now - phaseStart >= inertia::kPreUs) {
    health.slow_age_us = static_cast<uint32_t>(now - slowTime);
    const char* bad = inertia::unhealthy(settings, health);
    if (bad) { finish(Phase::Fault, bad); return; }
    if (fabsf(health.rpm) > 10 || fabsf(health.current_mA) > 10) {
      phase = Phase::Settling; phaseStart = now; stillSince = 0; reason = "waiting_for_stop"; return;
    }
    const int16_t cmd = inertia::command(settings, pulseIndex);
    const bool set = write32(0xB0, static_cast<int32_t>(cmd) * 100);
    // The interlock may have arrived while the setpoint write was in progress.
    if (!set || stopRequested.load()) { finish(Phase::Fault, "pulse_start_cancelled_or_failed"); return; }
    const bool enabled = write8(0x00, 1);
    pulses[pulseIndex].on = relative(clockUs());
    pulses[pulseIndex].current = cmd;
    if (!enabled) { finish(Phase::Fault, "enable_write_failed"); return; }
    commandMa = cmd; output = true; stopConfirmed = false;
    phase = Phase::Pulse; phaseStart = clockUs(); reason = "current_pulse";
    pulseDeadline = runStart + pulses[pulseIndex].on + settings.duration_ms * 1000ULL;
  }
  if (phase == Phase::Post && now - phaseStart >= inertia::kPostUs) {
    pulses[pulseIndex].complete = true;
    if (pulseIndex + 1 == inertia::pulseCount(settings)) {
      finish(Phase::Done, "completed");
    } else {
      ++pulseIndex; phase = Phase::Settling; phaseStart = now; stillSince = 0;
      reason = "waiting_for_stop";
    }
  }
  if (!inertia::active(phase) && !stopConfirmed && now - lastStopRetry >= 20000) motorOff();
}

void sample(uint64_t scheduled) {
  Row r{}; const uint64_t begin = clockUs();
  r.late = static_cast<uint32_t>(begin > scheduled ? begin - scheduled : 0);
  r.speed_start = relative(clockUs());
  bool speedOk = read32(0x60, speedRaw); r.speed_end = relative(clockUs());
  serviceTimers();
  r.current_start = relative(clockUs());
  bool currentOk = read32(0xC0, currentRaw); r.current_end = relative(clockUs());
  serviceTimers();
  bool slowOk = true;
  if (!slowTime || clockUs() - slowTime >= 100000) {
    int32_t vin = 0, temp = 0;
    slowOk = read32(0x34, vin); serviceTimers();
    slowOk = read32(0x38, temp) && slowOk; serviceTimers();
    slowOk = read8(0x0C, motorStatus) && slowOk; serviceTimers();
    slowOk = read8(0x0D, motorError) && slowOk; serviceTimers();
    if (slowOk) {
      health.voltage_mV = static_cast<uint16_t>(constrain(vin * 10, 0, 65535));
      health.temperature_C = static_cast<int16_t>(temp); health.error = motorError; slowTime = clockUs();
    }
  }
  health.reads_ok = speedOk && currentOk && slowOk;
  health.rpm = speedOk ? speedRaw / 100.0f : NAN;
  health.current_mA = currentOk ? currentRaw / 100.0f : NAN;
  health.slow_age_us = static_cast<uint32_t>(clockUs() - slowTime);
  if (inertia::active(phase)) {
    const char* bad = inertia::unhealthy(settings, health);
    if (bad) finish(Phase::Fault, bad);
    if (phase == Phase::Settling) {
      if (fabsf(health.rpm) <= 10 && fabsf(health.current_mA) <= 10) {
        if (!stillSince) stillSince = clockUs();
      } else stillSince = 0;
    }
    if (phase == Phase::Pre && (fabsf(health.rpm) > 10 || fabsf(health.current_mA) > 10)) {
      phase = Phase::Settling; phaseStart = clockUs(); stillSince = 0; reason = "waiting_for_stop";
    }
  }
  r.t = relative(clockUs()); r.slow_t = relative(slowTime);
  r.speed_raw = speedRaw; r.current_raw = currentRaw;
  r.voltage = health.voltage_mV; r.temperature = health.temperature_C;
  r.command = commandMa; r.pulse = pulseIndex + 1; r.phase = static_cast<uint8_t>(phase);
  r.valid = (speedOk ? 1 : 0) | (currentOk ? 2 : 0) | (slowOk && health.slow_age_us <= 250000 ? 4 : 0);
  r.status = motorStatus; r.error = motorError; r.output = output;
  r.i2c_errors = errors; r.missed = missed; r.work = clockUs() - begin;
  // Retain the final failing/stop sample too; completed runs freeze here.
  if (runStart && !logFrozen) {
    if (rowCount < inertia::kMaxRows) rows[rowCount++] = r;
    else if (inertia::active(phase)) finish(Phase::Fault, "log_full");
    if (!inertia::active(phase)) logFrozen = true;
  }
  publish();
}

void ownerTask(void*) {
  Wire.begin(2, 1, 400000); Wire.setTimeOut(3);
  initializeMotor(); reason = initialized ? "ready" : "motor_initialization_failed";
  nextSample = clockUs();
  for (;;) {
    serviceTimers();
    Request req;
    if (xQueueReceive(requests, &req, 0) == pdTRUE) {
      if (req.type == RequestType::Clear && !inertia::active(phase)) {
        initializeMotor(); rowCount = 0; runStart = 0; pulseIndex = 0; pulseDeadline = 0; logFrozen = true;
        phase = Phase::Idle; reason = initialized ? "ready" : "motor_initialization_failed";
      } else if (req.type == RequestType::Start && !inertia::active(phase) && !rowCount) {
        settings = req.settings;
        health.slow_age_us = static_cast<uint32_t>(clockUs() - slowTime);
        const char* bad = inertia::unhealthy(settings, health);
        if (!initialized || !stopConfirmed || !rows) reason = "motor_or_memory_not_ready";
        else if (const char* invalid = inertia::validate(settings)) reason = invalid;
        else if (bad) reason = bad;
        else {
          memset(pulses, 0, sizeof(pulses)); ++runCounter; pulseIndex = 0; missed = 0;
          runStart = clockUs(); phaseStart = runStart; stillSince = 0; logFrozen = false;
          phase = Phase::Settling; reason = "waiting_for_stop";
        }
      } else reason = "request_rejected_preserve_log";
      publish(); pending.store(false);
    }
    const uint64_t now = clockUs();
    if (now >= nextSample) {
      const uint64_t scheduled = nextSample;
      missed += static_cast<uint32_t>((now - scheduled) / inertia::kSampleUs);
      nextSample = scheduled + ((now - scheduled) / inertia::kSampleUs + 1) * inertia::kSampleUs;
      const bool wasActive = inertia::active(phase);
      sample(scheduled);
      if (wasActive && !inertia::active(phase)) publish();
    }
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

bool integerArg(const char* key, long& value) {
  if (!server.hasArg(key)) return false;
  String a = server.arg(key); char* end = nullptr; value = strtol(a.c_str(), &end, 10);
  return a.length() && end && *end == 0;
}
String jsonFloat(float v) { return isfinite(v) ? String(v, 2) : "null"; }
void statusHandler() {
  Snapshot s = state();
  String j = "{\"revision\":\"" + String(kRevision) + "\",\"phase\":\"" + inertia::name(s.phase) +
    "\",\"reason\":\"" + s.reason + "\",\"active\":" + (inertia::active(s.phase) ? "true" : "false") +
    ",\"ready\":" + (s.ready ? "true" : "false") + ",\"pending\":" + (pending.load() ? "true" : "false") +
    ",\"stop_confirmed\":" + (s.stop_confirmed ? "true" : "false") +
    ",\"frozen\":" + (s.frozen ? "true" : "false") +
    ",\"rpm\":" + jsonFloat(s.rpm) + ",\"current_mA\":" + jsonFloat(s.current) +
    ",\"voltage_mV\":" + s.voltage + ",\"temperature_C\":" + s.temperature +
    ",\"rows\":" + s.rows + ",\"missed\":" + s.missed + ",\"i2c_errors\":" + s.errors +
    ",\"pulse\":" + s.pulse + ",\"total\":" + s.total + ",\"run\":" + s.run + "}";
  server.sendHeader("Cache-Control", "no-store"); server.send(200, "application/json", j);
}
void startHandler() {
  Snapshot s = state();
  if (inertia::active(s.phase) || s.rows || !s.ready || pending.load()) {
    server.send(409, "text/plain", "測定中、ログ保存待ち、またはモータ未準備です。"); return;
  }
  Request req{}; req.type = RequestType::Start; Settings& c = req.settings;
  if (server.arg("fixed") != "1" || digitalRead(41) == LOW) {
    server.send(400, "text/plain", "機体の固定を確認し、本体ボタンを離してください。"); return;
  }
  const String mode = server.arg("mode");
  if (mode != "single" && mode != "batch") {
    server.send(400, "text/plain", "modeが不正です。"); return;
  }
  c.single = mode == "single";
  const String label = server.arg("label");
  if (!label.length() || label.length() >= sizeof(c.label)) {
    server.send(400, "text/plain", "記録名は英数字・_・-で1〜24文字です。"); return;
  }
  for (unsigned i = 0; i < label.length(); ++i)
    if (!isalnum(static_cast<unsigned char>(label[i])) && label[i] != '_' && label[i] != '-') {
      server.send(400, "text/plain", "記録名の文字が不正です。"); return;
    }
  strlcpy(c.label, label.c_str(), sizeof(c.label));
  long n;
  if (!integerArg("duration", n) || n < 50 || n > 500) { server.send(400, "text/plain", "入力時間は50〜500 msです。"); return; }
  c.duration_ms = n;
  if (!integerArg("repeats", n) || n < 1 || n > 5) { server.send(400, "text/plain", "反復は1〜5です。"); return; }
  c.repeats = n;
  if (!integerArg("limit", n) || n < 300 || n > 2500) { server.send(400, "text/plain", "回転数上限は300〜2500 rpmです。"); return; }
  c.rpm_limit = n;
  if (!integerArg("voltage", n) || n < 5000 || n > 12000) { server.send(400, "text/plain", "電圧下限が不正です。"); return; }
  c.min_voltage_mV = n;
  if (!integerArg("current", n) || n == 0 || n < -300 || n > 300) { server.send(400, "text/plain", "単発電流は±1〜300 mAです。"); return; }
  c.single_mA = n;
  for (unsigned i = 0; i < 3; ++i) {
    String key = "level" + String(i + 1);
    if (!integerArg(key.c_str(), n) || n < 30 || n > 300) { server.send(400, "text/plain", "電流段階は30〜300 mAです。"); return; }
    c.levels_mA[i] = n;
  }
  if (const char* bad = inertia::validate(c)) { server.send(400, "text/plain", bad); return; }
  pending.store(true);
  if (xQueueSend(requests, &req, 0) != pdTRUE) { pending.store(false); server.send(503, "text/plain", "command_queue_full"); return; }
  server.send(202, "text/plain", "開始要求を受け付けました。");
}

void csvHandler() {
  Snapshot s = state();
  if (inertia::active(s.phase) || pending.load() || !s.rows || !s.frozen) {
    server.send(409, "text/plain", "測定終了後に保存してください。"); return;
  }
  String filename = "motor_response_" + String(settings.label) + "_" + String(bootId, HEX) + "_" + String(runCounter) + ".csv";
  server.sendHeader("Content-Disposition", "attachment; filename=\"" + filename + "\"");
  server.sendHeader("Cache-Control", "no-store");
  server.setContentLength(CONTENT_LENGTH_UNKNOWN); server.send(200, "text/csv; charset=utf-8", "");
  String header = "# schema=atoms3r_inertia_v1\n# revision=" + String(kRevision) +
    "\n# configuration=current_wheel_and_weights" +
    "\n# label=" + settings.label + "\n# run_id=" + String(bootId, HEX) + "_" + String(runCounter) +
    "\n# result=" + inertia::name(s.phase) + "\n# reason=" + s.reason +
    "\n# sample_count=" + String(s.rows) + "\n# expected_pulses=" + String(inertia::pulseCount(settings)) +
    "\n# pulse_width_ms=" + String(settings.duration_ms) + "\n# repeats=" + String(settings.repeats) +
    "\n# rpm_limit=" + String(settings.rpm_limit) + "\n# min_voltage_mV=" + String(settings.min_voltage_mV) +
    "\n# sample_period_us=10000\n# motor_frame=raw_driver_current_and_encoder_rpm\n# body_condition=rigidly_clamped_required\n# slow_status_period_us=100000\n";
  server.sendContent(header);
  server.sendContent("sample,t_us,pulse_id,phase,driver_cmd_mA,output_enabled,rpm,actual_current_mA,speed_start_us,speed_end_us,current_start_us,current_end_us,voltage_mV,temperature_C,slow_t_us,speed_valid,current_valid,status_valid,motor_status,motor_error,i2c_errors,missed_samples,sample_late_us,sample_work_us,pulse_on_us,pulse_zero_us,pulse_off_us,pulse_complete,pulse_cmd_mA\n");
  String chunk; chunk.reserve(4096);
  for (uint32_t i = 0; i < s.rows && server.client().connected(); ++i) {
    const Row& r = rows[i]; const Pulse& p = pulses[r.pulse - 1]; char line[640];
    snprintf(line, sizeof(line), "%lu,%lu,%u,%s,%d,%u,%.2f,%.2f,%lu,%lu,%lu,%lu,%u,%d,%lu,%u,%u,%u,%u,%u,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%u,%d\n",
      (unsigned long)i, (unsigned long)r.t, r.pulse, inertia::name(static_cast<Phase>(r.phase)), r.command, r.output,
      r.speed_raw / 100.0, r.current_raw / 100.0,
      (unsigned long)r.speed_start, (unsigned long)r.speed_end, (unsigned long)r.current_start, (unsigned long)r.current_end,
      r.voltage, r.temperature, (unsigned long)r.slow_t, !!(r.valid & 1), !!(r.valid & 2), !!(r.valid & 4), r.status, r.error,
      (unsigned long)r.i2c_errors, (unsigned long)r.missed, (unsigned long)r.late, (unsigned long)r.work,
      (unsigned long)p.on, (unsigned long)p.zero, (unsigned long)p.off, p.complete, p.current);
    chunk += line;
    if (chunk.length() >= 3500) { server.sendContent(chunk); chunk = ""; }
  }
  if (chunk.length()) server.sendContent(chunk);
  server.sendContent("");
}

void setup() {
  Serial.begin(115200);
  auto cfg = M5.config(); cfg.serial_baudrate = 0; cfg.internal_imu = false; M5.begin(cfg);
  pinMode(41, INPUT_PULLUP);
  bootId = esp_random();
  rows = static_cast<Row*>(heap_caps_calloc(inertia::kMaxRows, sizeof(Row), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  requests = xQueueCreate(1, sizeof(Request));
  // Core 1 owns every motor transaction. HTTP transfers run at lower priority.
  if (!requests || xTaskCreatePinnedToCore(ownerTask, "inertia-motor", 8192, nullptr, 4, nullptr, 1) != pdPASS) {
    // A failed allocation must still leave the motor disabled and the Web
    // diagnostics accessible. No owner task exists in this branch.
    Wire.begin(2, 1, 400000); Wire.setTimeOut(3); motorOff();
    reason = "motor_task_creation_failed"; publish();
  }
  WiFi.mode(WIFI_AP); WiFi.softAP("AtomS3R_Inertia", "12345678");
  server.on("/", HTTP_GET, [] { server.sendHeader("Cache-Control", "no-store"); server.send_P(200, "text/html; charset=utf-8", kWebPage); });
  server.on("/status", HTTP_GET, statusHandler);
  server.on("/start", HTTP_POST, startHandler);
  server.on("/stop", HTTP_POST, [] { stopRequested.store(true); server.send(202, "text/plain", "停止要求を受け付けました。"); });
  server.on("/clear", HTTP_POST, [] {
    if (!requests) { server.send(503, "text/plain", "motor_task_unavailable"); return; }
    if (inertia::active(state().phase) || pending.exchange(true)) { server.send(409, "text/plain", "測定中または要求処理中です。"); return; }
    Request r{}; r.type = RequestType::Clear;
    if (xQueueSend(requests, &r, 0) != pdTRUE) { pending.store(false); server.send(503, "text/plain", "command_queue_full"); return; }
    server.send(202, "text/plain", "ログ消去・モータ停止を要求しました。");
  });
  server.on("/data.csv", HTTP_GET, csvHandler);
  server.begin();
  Serial.println("AtomS3R inertia v1; Wi-Fi AtomS3R_Inertia / 12345678; http://192.168.4.1/");
}
void loop() {
  M5.update(); if (M5.BtnA.wasPressed()) stopRequested.store(true);
  if (Serial.available()) { const char c = Serial.read(); if (c == '!' || c == 's') stopRequested.store(true); }
  server.handleClient(); delay(2);
}
