/*
 * mount_control.ino
 * Firmware pengontrol mount Alt-Azimuth teleskop radio portabel
 * Arduino Uno + 2x TB6600 + 2x NEMA 23  (library: AccelStepper, mode DRIVER)
 *
 * WIRING TB6600 (common-cathode):
 *   PUL- , DIR- , ENA-  -> GND Arduino
 *   Azimuth : PUL+ -> D2 , DIR+ -> D3
 *   Elevasi : PUL+ -> D4 , DIR+ -> D5
 *   ENA+ dibiarkan tidak tersambung (TB6600 aktif secara default).
 *
 * PROTOKOL SERIAL (115200 baud, ASCII, diakhiri '\n'):
 *   M <Az_Steps> <El_Steps>  -> gerak ke posisi absolut (dalam step)
 *   STOP                     -> hentikan kedua motor seketika  (balasan: STOPPED)
 *   P                        -> minta posisi sekarang          (balasan: POS <az> <el>)
 *   V <Az_sps> <El_sps>      -> atur kecepatan maks (step/detik) (balasan: SPEED <az> <el>)
 * Balasan "OK" dikirim satu kali saat kedua motor sudah tiba di target.
 */

#include <AccelStepper.h>

// ----------------------------- KONFIGURASI ---------------------------------
const uint8_t AZ_STEP_PIN = 2;
const uint8_t AZ_DIR_PIN  = 3;
const uint8_t EL_STEP_PIN = 4;
const uint8_t EL_DIR_PIN  = 5;

const long  BAUD_RATE    = 115200;
const float AZ_MAX_SPEED    = 1200.0;  // step/detik awal (diganti oleh perintah V dari Python)
const float EL_MAX_SPEED    = 889.0;
const float MAX_SPEED_LIMIT = 3000.0;  // batas keras step/detik per motor (keamanan)
const float ACCEL_RAMP_S    = 1.0;     // detik untuk akselerasi 0 -> kecepatan maks
const unsigned int MIN_PULSE_US = 10;  // lebar pulsa (us), TB6600 butuh >= 2.5 us

// ------------------------------- OBJEK --------------------------------------
AccelStepper azMotor(AccelStepper::DRIVER, AZ_STEP_PIN, AZ_DIR_PIN);
AccelStepper elMotor(AccelStepper::DRIVER, EL_STEP_PIN, EL_DIR_PIN);

const uint8_t BUF_SIZE = 40;
char    rxBuf[BUF_SIZE];
uint8_t rxLen      = 0;
bool    rxOverflow = false;
bool    moving     = false;   // true = ada perintah M yang belum dibalas OK

// ------------------------------ FUNGSI --------------------------------------
void sendPosition() {
  Serial.print(F("POS "));
  Serial.print(azMotor.currentPosition());
  Serial.print(' ');
  Serial.println(elMotor.currentPosition());
}

void emergencyStop() {
  // setCurrentPosition() me-nol-kan kecepatan seketika (tanpa deselerasi)
  // sambil mempertahankan hitungan posisi.
  azMotor.setCurrentPosition(azMotor.currentPosition());
  elMotor.setCurrentPosition(elMotor.currentPosition());
  azMotor.moveTo(azMotor.currentPosition());
  elMotor.moveTo(elMotor.currentPosition());
  moving = false;
  Serial.println(F("STOPPED"));
}

void applySpeed(AccelStepper &m, float sps) {
  if (sps < 1.0) sps = 1.0;
  if (sps > MAX_SPEED_LIMIT) sps = MAX_SPEED_LIMIT;
  m.setMaxSpeed(sps);
  m.setAcceleration(sps / ACCEL_RAMP_S);
}

void handleLine(char *line) {
  if (strcmp(line, "STOP") == 0) {
    emergencyStop();
    return;
  }
  if (strcmp(line, "P") == 0) {
    sendPosition();
    return;
  }
  if (line[0] == 'V' && line[1] == ' ') {
    char *p1;
    char *p2;
    long azSps = strtol(line + 2, &p1, 10);
    if (p1 == line + 2) { Serial.println(F("ERR ARG")); return; }
    long elSps = strtol(p1, &p2, 10);
    if (p2 == p1)       { Serial.println(F("ERR ARG")); return; }
    applySpeed(azMotor, (float)azSps);
    applySpeed(elMotor, (float)elSps);
    Serial.print(F("SPEED "));
    Serial.print((long)azMotor.maxSpeed());
    Serial.print(' ');
    Serial.println((long)elMotor.maxSpeed());
    return;
  }
  if (line[0] == 'M' && line[1] == ' ') {
    char *p1;
    char *p2;
    long azTarget = strtol(line + 2, &p1, 10);
    if (p1 == line + 2) { Serial.println(F("ERR ARG")); return; }
    long elTarget = strtol(p1, &p2, 10);
    if (p2 == p1)       { Serial.println(F("ERR ARG")); return; }
    azMotor.moveTo(azTarget);
    elMotor.moveTo(elTarget);
    moving = true;
    return;
  }
  Serial.println(F("ERR CMD"));
}

void readSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxOverflow) {
        Serial.println(F("ERR LEN"));
      } else if (rxLen > 0) {
        rxBuf[rxLen] = '\0';
        handleLine(rxBuf);
      }
      rxLen = 0;
      rxOverflow = false;
    } else if (rxLen < BUF_SIZE - 1) {
      rxBuf[rxLen++] = c;
    } else {
      rxOverflow = true;
    }
    // jaga agar pulsa step tetap keluar walau sedang menerima data
    azMotor.run();
    elMotor.run();
  }
}

// ------------------------------- SETUP --------------------------------------
void setup() {
  Serial.begin(BAUD_RATE);

  applySpeed(azMotor, AZ_MAX_SPEED);
  azMotor.setMinPulseWidth(MIN_PULSE_US);

  applySpeed(elMotor, EL_MAX_SPEED);
  elMotor.setMinPulseWidth(MIN_PULSE_US);

  azMotor.setCurrentPosition(0);
  elMotor.setCurrentPosition(0);

  Serial.println(F("READY"));
}

// -------------------------------- LOOP --------------------------------------
void loop() {
  readSerial();          // non-blocking

  azMotor.run();         // non-blocking, maksimal 1 step per panggilan
  elMotor.run();

  if (moving && azMotor.distanceToGo() == 0 && elMotor.distanceToGo() == 0) {
    moving = false;
    Serial.println(F("OK"));
  }
}
