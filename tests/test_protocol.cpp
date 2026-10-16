#include "../src/protocol.h"
#include <assert.h>
#include <set>
int main() {
  using namespace inertia;
  Settings s;
  assert(validate(s) == nullptr);
  s.duration_ms = 501; assert(validate(s)); s.duration_ms = 400;
  s.single_mA = 0; assert(validate(s)); s.single_mA = 100;
  s.single = false;
  assert(pulseCount(s) == 18);
  for (unsigned repetition = 0; repetition < 3; ++repetition) {
    std::set<int> commands;
    for (unsigned i = repetition * 6; i < (repetition + 1) * 6; ++i) commands.insert(command(s, i));
    assert(commands == std::set<int>({-150,-100,-50,50,100,150}));
  }
  Health h; assert(!unhealthy(s,h));
  h.rpm = 1500; assert(unhealthy(s,h)); h.rpm = -1500; assert(unhealthy(s,h)); h.rpm=0;
  h.voltage_mV=5999; assert(unhealthy(s,h)); h.voltage_mV=7400;
  h.reads_ok=false; assert(unhealthy(s,h)); h.reads_ok=true;
  h.slow_age_us=250001; assert(unhealthy(s,h)); h.slow_age_us=0;
  h.error=1; assert(unhealthy(s,h)); h.error=0;
  h.rpm=NAN; assert(unhealthy(s,h));
  assert(!due(1000,0)); assert(!due(999,1000)); assert(due(1000,1000));
  assert(!stoppedLongEnough(2000000,0)); assert(!stoppedLongEnough(1000000,1));
  assert(stoppedLongEnough(1000001,1));
  // Regression: pulse-end I2C writes advanced the clock after 'now' was cached.
  // A start 500 us in the future must not make a two-second POST complete.
  const uint64_t cached_now = 2000000, post_entered = cached_now + 500;
  assert(!elapsed(cached_now, post_entered, kPostUs));
  assert(!elapsed(post_entered + kPostUs - 1, post_entered, kPostUs));
  assert(elapsed(post_entered + kPostUs, post_entered, kPostUs));
  assert(!stoppedLongEnough(cached_now, cached_now + 1));
  return 0;
}
