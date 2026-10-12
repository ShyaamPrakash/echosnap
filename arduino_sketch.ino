// max4466 mic on a0 -> 250000 baud
// data line format (never changes): ms,rms,zcr,peak,b1..b8
// anything else the device wants to say goes on a '#' comment line
#include <math.h>

const uint8_t  NB = 8;
const uint16_t N  = 128;        // samples per window
const float    FS = 9615.0;     // adc free-run rate
const float    BAND_HZ[NB] = {200, 400, 600, 900, 1300, 1800, 2500, 3500};
const int16_t  DEADBAND = 3;    // ignore tiny wiggles for zcr
const uint8_t  LED = 13;        // only output pin: on while a window is processing

enum { FREE = 0, FILLING = 1, READY = 2, BUSY = 3 };

volatile int16_t  bufs[2][N];
volatile uint8_t  bufState[2] = {FILLING, FREE};
volatile uint8_t  fillBuf = 0;
volatile uint16_t fillIdx = 0;
volatile uint16_t overruns = 0;   // windows lost because the loop fell behind

float coeff[NB];
uint32_t lastOverrunReport = 0;
uint16_t lastOverrunCount = 0;

ISR(ADC_vect) {
  bufs[fillBuf][fillIdx++] = ADC;
  if (fillIdx >= N) {
    fillIdx = 0;
    uint8_t other = fillBuf ^ 1;
    if (bufState[other] == FREE) {
      bufState[fillBuf] = READY;
      bufState[other]   = FILLING;
      fillBuf = other;
    } else {
      overruns++;   // other buffer still busy, this window is dropped
    }
  }
}

void setup() {
  Serial.begin(250000);
  pinMode(LED, OUTPUT);
  for (uint8_t k = 0; k < NB; k++) {
    coeff[k] = 2.0 * cos(2.0 * PI * BAND_HZ[k] / FS);
  }
  Serial.println(F("# acoustic sentinel ready"));

  DIDR0 |= _BV(ADC0D);
  ADMUX  = _BV(REFS0);                          // avcc ref, channel a0
  ADCSRB = 0;                                   // free running
  ADCSRA = _BV(ADEN) | _BV(ADSC) | _BV(ADATE) | _BV(ADIE) |
           _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0); // prescaler 128
  sei();
}

void process(uint8_t b) {
  int16_t *x = (int16_t *)bufs[b];
  uint32_t ms = millis();

  // remove dc offset
  long sum = 0;
  for (uint16_t i = 0; i < N; i++) sum += x[i];
  int16_t mean = (int16_t)(sum / (long)N);

  long sq = 0;
  int16_t peak = 0;
  uint16_t cross = 0;
  int8_t state = 0;
  for (uint16_t i = 0; i < N; i++) {
    int16_t v = x[i] - mean;
    x[i] = v;
    int16_t a = v < 0 ? -v : v;
    if (a > peak) peak = a;
    sq += (long)v * v;
    int8_t s = (v > DEADBAND) ? 1 : ((v < -DEADBAND) ? -1 : 0);
    if (s != 0) {
      if (state != 0 && s != state) cross++;
      state = s;
    }
  }
  float rms = sqrt((float)sq / N);
  float zcr = (float)cross / N;

  // goertzel per band
  float band[NB];
  for (uint8_t k = 0; k < NB; k++) {
    float c = coeff[k], s1 = 0, s2 = 0;
    for (uint16_t i = 0; i < N; i++) {
      float s = (float)x[i] + c * s1 - s2;
      s2 = s1;
      s1 = s;
    }
    float power = s1 * s1 + s2 * s2 - c * s1 * s2;
    if (power < 0) power = 0;
    band[k] = 2.0 * sqrt(power) / N;
  }

  Serial.print(ms);      Serial.print(',');
  Serial.print(rms, 1);  Serial.print(',');
  Serial.print(zcr, 3);  Serial.print(',');
  Serial.print(peak);
  for (uint8_t k = 0; k < NB; k++) {
    Serial.print(',');
    Serial.print(band[k], 1);
  }
  Serial.println();

  // overrun count, reported only when it changes, on its own comment line
  if (ms - lastOverrunReport > 2000) {
    lastOverrunReport = ms;
    noInterrupts(); uint16_t ov = overruns; interrupts();
    if (ov != lastOverrunCount) {
      lastOverrunCount = ov;
      Serial.print(F("# overruns=")); Serial.println(ov);
    }
  }
}

void loop() {
  int8_t b = -1;
  noInterrupts();
  if (bufState[0] == READY) b = 0;
  else if (bufState[1] == READY) b = 1;
  if (b >= 0) bufState[b] = BUSY;
  interrupts();

  if (b < 0) return;
  digitalWrite(LED, HIGH);   // lit while a window is being processed
  process(b);
  digitalWrite(LED, LOW);

  noInterrupts();
  bufState[b] = FREE;
  interrupts();
}
