// EchoSnap firmware v2 - MAX4466 mic on A0 -> 250000 baud
//
// data line format: ms,rms,zcr,peak,b1..b8,domhz,tonal

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifdef ARDUINO
  #include <Arduino.h>
  #include <avr/io.h>
  #include <avr/interrupt.h>
#else
  // ---------------------------------------------------------------------------
  // VS Code IntelliSense Mock Block (keeps VS Code 100% error-free without
  // needing c_cpp_properties.json or Arduino includePath configured)
  // ---------------------------------------------------------------------------
  #ifndef PI
    #define PI 3.14159265358979323846
  #endif
  #define HIGH 0x1
  #define LOW  0x0
  #define OUTPUT 0x1
  #define F(str) (str)
  #define _BV(bit) (1 << (bit))
  #define ISR(vector, ...) void vector(void)
  #define cli()
  #define sei()
  #define noInterrupts()
  #define interrupts()

  extern volatile uint8_t TCCR1A, TCCR1B, TCNT1, TIFR1, DIDR0, ADMUX, ADCSRB, ADCSRA;
  extern volatile uint16_t OCR1A, OCR1B, ADC;
  #define OCF1B 2
  #define WGM12 3
  #define CS11  1
  #define ADC0D 0
  #define REFS0 6
  #define ADTS2 2
  #define ADTS0 0
  #define ADEN  7
  #define ADATE 5
  #define ADIE  3
  #define ADPS2 2
  #define ADPS1 1
  #define ADPS0 0

  inline void pinMode(uint8_t, uint8_t) {}
  inline void digitalWrite(uint8_t, uint8_t) {}
  inline uint32_t millis(void) { return 0; }
  inline uint32_t micros(void) { return 0; }
  inline char* ultoa(unsigned long val, char* s, int radix) { (void)val; (void)radix; return s; }

  struct MockSerial {
    void begin(unsigned long) {}
    void print(const char*) {}
    void print(uint16_t) {}
    void println(const char*) {}
    void println(uint16_t) {}
    void write(const uint8_t*, size_t) {}
    int available(void) { return 0; }
    int read(void) { return -1; }
  };
  static MockSerial Serial;
#endif

const uint8_t  NB = 8;
const uint8_t  N  = 128;            // samples per window (fft code assumes 128)
const uint8_t  M  = N / 2;          // the fft itself runs on 64 complex points
const float    FS = 8000.0f;        // sample rate, set by timer1 below
const float    BIN_HZ = FS / N;     // 62.5 Hz per fft bin
const int16_t  DEADBAND = 3;        // ignore tiny wiggles for zcr
const uint8_t  LED = 13;            // on while a window is processing

// fft bins summed into each band (bin k is centred on k * 62.5 Hz)
//   60-125, 190-250, 310-440, 500-690, 750-1060, 1120-1620, 1690-2500, 2560-3940 Hz
const uint8_t BAND_LO[NB] = {1, 3, 5,  8, 12, 18, 27, 41};
const uint8_t BAND_HI[NB] = {2, 4, 7, 11, 17, 26, 40, 63};

enum { FREE = 0, FILLING = 1, READY = 2, BUSY = 3 };

volatile int16_t  bufs[2][N];
volatile uint8_t  bufState[2] = {FILLING, FREE};
volatile uint8_t  fillBuf = 0;
volatile uint8_t  fillIdx = 0;
volatile uint16_t overruns = 0;     // windows lost because the loop fell behind

int16_t  im[M];                     // fft imaginary part (real part reuses the sample buffer)
int16_t  sinQ[N * 3 / 4 + 1];       // sin(2*pi*k/N) in q15; cos(a) = sinQ[k + N/4]
int16_t  hann[N / 2 + 1];           // hann window in q15, symmetric so only half is stored
uint32_t pw[M];                     // per-bin power, before scaling

uint32_t lastReport = 0;
uint32_t busyUs = 0;                // time spent in process() since the last report
uint32_t ledUntil = 0;

ISR(ADC_vect) {
  TIFR1 = _BV(OCF1B);   // clear the timer flag so the next compare match can trigger again
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
  for (uint8_t k = 0; k <= N * 3 / 4; k++) {
    sinQ[k] = (int16_t)lround(32767.0 * sin(2.0 * PI * k / N));
  }
  for (uint8_t i = 0; i <= N / 2; i++) {
    hann[i] = (int16_t)lround(32767.0 * 0.5 * (1.0 - cos(2.0 * PI * i / N)));
  }
  Serial.println(F("# EchoSnap ready"));

  // timer1: ctc mode, 16 MHz / 8 / 250 = 8000 compare matches per second
  cli();
  TCCR1A = 0;
  TCCR1B = _BV(WGM12) | _BV(CS11);
  OCR1A  = 249;
  OCR1B  = 249;
  TCNT1  = 0;

  // adc: avcc ref, channel a0, started by timer1 compare match b, interrupt per sample
  DIDR0 |= _BV(ADC0D);
  ADMUX  = _BV(REFS0);
  ADCSRB = _BV(ADTS2) | _BV(ADTS0);
  ADCSRA = _BV(ADEN) | _BV(ADATE) | _BV(ADIE) |
           _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0);
  sei();
}

int16_t __attribute__((noinline)) mac15(int16_t a, int16_t b, int16_t c, int16_t d) {
  int32_t acc = (int32_t)a * b + (int32_t)c * d;
  return (int16_t)(acc >> 16) << 1;
}

uint32_t __attribute__((noinline)) sumsq16(int16_t a, int16_t b) {
  return (uint32_t)((int32_t)a * a) + (uint32_t)((int32_t)b * b);
}

uint8_t __attribute__((optimize("O2"))) fft(int16_t *re, int16_t *imv) {
  for (uint8_t i = 1, j = 0; i < M; i++) {
    uint8_t bit = M >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      int16_t t = re[i]; re[i] = re[j]; re[j] = t;
      t = imv[i]; imv[i] = imv[j]; imv[j] = t;
    }
  }

  uint8_t exp2 = 0;
  for (uint8_t len = 2, step = N / 2; len <= M; len <<= 1, step >>= 1) {
    bool big = false;
    for (uint8_t i = 0; i < M; i++) {
      if (re[i] > 13000 || re[i] < -13000 || imv[i] > 13000 || imv[i] < -13000) { big = true; break; }
    }
    if (big) {
      for (uint8_t i = 0; i < M; i++) { re[i] >>= 1; imv[i] >>= 1; }
      exp2++;
    }
    uint8_t half = len >> 1;
    for (uint8_t k = 0; k < half; k++) {
      int16_t c = sinQ[k * step + N / 4];
      int16_t s = sinQ[k * step];
      for (uint8_t i = k; i < M; i += len) {
        uint8_t j = i + half;
        int16_t xr = re[j], xi = imv[j];
        int16_t tr, ti;
        if (k == 0) { tr = xr; ti = xi; }
        else {
          tr = mac15(c, xr, s, xi);
          ti = mac15(c, xi, -s, xr);
        }
        int16_t ur = re[i], ui = imv[i];
        re[j] = ur - tr;  imv[j] = ui - ti;
        re[i] = ur + tr;  imv[i] = ui + ti;
      }
    }
  }
  return exp2;
}

char *putFixed(char *p, uint16_t v, uint8_t dec) {
  char tmp[6];
  uint8_t n = 0;
  do { tmp[n++] = '0' + v % 10; v /= 10; } while (v || n <= dec);
  while (n) {
    if (n == dec && dec) *p++ = '.';
    *p++ = tmp[--n];
  }
  return p;
}

static inline uint16_t clamp16(float v) {
  return v <= 0.0f ? 0 : (v >= 65535.0f ? 65535 : (uint16_t)(v + 0.5f));
}

void process(uint8_t b) {
  int16_t *x = (int16_t *)bufs[b];
  uint32_t ms = millis();

  int32_t sum = 0;
  for (uint8_t i = 0; i < N; i++) sum += x[i];
  int16_t mean = (int16_t)(sum / (int32_t)N);

  uint32_t sq = 0;
  int32_t sumv = 0;                 // sum of (sample - mean): fixes rms rounding error on quiet sounds
  int16_t peak = 0;
  uint8_t cross = 0;
  int8_t state = 0;
  for (uint8_t i = 0; i < N; i++) {
    int16_t v = x[i] - mean;
    int16_t a = v < 0 ? -v : v;
    if (a > peak) peak = a;
    sq += (uint32_t)((int32_t)v * v);
    sumv += v;
    int8_t s = (v > DEADBAND) ? 1 : ((v < -DEADBAND) ? -1 : 0);
    if (s != 0) {
      if (state != 0 && s != state) cross++;
      state = s;
    }
    x[i] = (int16_t)(((int32_t)(v * 32) * hann[i <= N / 2 ? i : N - i]) >> 16);
  }

  for (uint8_t n = 0; n < M; n++) {
    int16_t ev = x[2 * n], od = x[2 * n + 1];
    x[n] = ev;
    im[n] = od;
  }
  uint8_t e = fft(x, im);

  uint8_t kmax = 1;
  for (uint8_t k = 1; k < M; k++) {
    int16_t ar = x[k], ai = im[k], br = x[M - k], bi = im[M - k];
    int16_t er  = (int16_t)(((int32_t)ar + br) >> 1), ei = (int16_t)(((int32_t)ai - bi) >> 1);
    int16_t orr = (int16_t)(((int32_t)ai + bi) >> 1), oi = (int16_t)(((int32_t)br - ar) >> 1);
    int16_t c = sinQ[k + N / 4], s = sinQ[k];
    int16_t xr = (int16_t)(((int32_t)er + mac15(c, orr, s, oi)) >> 2);
    int16_t xi = (int16_t)(((int32_t)ei + mac15(c, oi, -s, orr)) >> 2);
    pw[k] = sumsq16(xr, xi);
    if (pw[k] > pw[kmax]) kmax = k;
  }

  float scale = (float)(ldexp(1.0, 2 * e + 4) / 786432.0);
  uint16_t band[NB];
  uint32_t total = 0;
  for (uint8_t bnd = 0; bnd < NB; bnd++) {
    uint32_t p = 0;
    for (uint8_t k = BAND_LO[bnd]; k <= BAND_HI[bnd]; k++) p += pw[k] >> 6;
    total += p;
    band[bnd] = clamp16(100.0f * (float)sqrt((double)p * 64.0 * scale));
  }

  float domhz = kmax * BIN_HZ;
  if (kmax > 1 && kmax < M - 1) {
    float a = (float)sqrt((double)pw[kmax - 1]);
    float c = (float)sqrt((double)pw[kmax]);
    float d = (float)sqrt((double)pw[kmax + 1]);
    float den = a - 2.0f * c + d;
    if (den < 0.0f) domhz = (kmax + 0.5f * (a - d) / den) * BIN_HZ;
  }
  uint32_t near = pw[kmax] >> 6;
  if (kmax > 1) near += pw[kmax - 1] >> 6;
  if (kmax < M - 1) near += pw[kmax + 1] >> 6;
  // exact rms: the integer mean above is rounded, which made quiet rooms read too loud and jumpy
  float mv = (float)sumv / N;
  float var = (float)sq / N - mv * mv;
  float rms = var > 0.0f ? (float)sqrt((double)var) : 0.0f;
  float tonal = total > 0 ? (float)near / (float)total : 0.0f;
  if (rms < 0.3f) { tonal = 0.0f; domhz = 0.0f; }

  char line[128];   // was 96: could overflow on loud sounds
  char *p = line;
  p = ultoa(ms, p, 10); p += strlen(p);
  *p++ = ','; p = putFixed(p, clamp16(rms * 10.0f), 1);
  *p++ = ','; p = putFixed(p, (uint16_t)((uint32_t)cross * 1000 / N), 3);
  *p++ = ','; p = putFixed(p, (uint16_t)peak, 0);
  for (uint8_t k = 0; k < NB; k++) { *p++ = ','; p = putFixed(p, band[k], 2); }
  *p++ = ','; p = putFixed(p, clamp16(domhz), 0);
  *p++ = ','; p = putFixed(p, clamp16(tonal * 100.0f), 2);
  *p++ = '\r'; *p++ = '\n';
  Serial.write((const uint8_t *)line, (size_t)(p - line));
}

void loop() {
  while (Serial.available()) {
    if (Serial.read() == 'L') ledUntil = millis() + 500;
  }

  int8_t b = -1;
  noInterrupts();
  if (bufState[0] == READY) b = 0;
  else if (bufState[1] == READY) b = 1;
  if (b >= 0) bufState[b] = BUSY;
  interrupts();

  if (b >= 0) {
    uint32_t t0 = micros();
    digitalWrite(LED, HIGH);
    process((uint8_t)b);
    digitalWrite(LED, millis() < ledUntil ? HIGH : LOW);
    busyUs += micros() - t0;

    noInterrupts();
    bufState[b] = FREE;
    interrupts();
  }

  digitalWrite(LED, millis() < ledUntil ? HIGH : LOW);

  uint32_t now = millis();
  if (now - lastReport >= 5000) {
    noInterrupts(); uint16_t ov = overruns; interrupts();
    Serial.print(F("# cpu="));
    Serial.print((uint16_t)(busyUs / ((now - lastReport) * 10UL)));
    Serial.print(F("% overruns="));
    Serial.println(ov);
    lastReport = now;
    busyUs = 0;
  }
}