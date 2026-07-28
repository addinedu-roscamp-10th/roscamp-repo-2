/*
 * Conveyor Belt Controller v5.0 — Serial Bridge (V6 Architecture)
 * Firmware tag: 1.7.0 (2026-05-08 — TOF2 코드 완전 제거. TOF1 단일 센서 운영)
 * Base: conveyor_controller v4.0.0
 *
 * 1.7.0 (2026-05-08, 사용자 요청):
 *   - TOF2 관련 코드 완전 제거 (1.5.x 시점 hardware 제거 후 1.6.0 까지 호환용 정의 유지했었음).
 *     · PIN_TOF2_RX, HardwareSerial tof2, dist2/raw2/det2/det2Start/buf2 globals 삭제
 *     · readSensors() TOF2 read + anti-crosstalk gating 삭제 (단일 센서이므로 무의미)
 *     · sim_exit 명령 + simExitUntilMs sticky 삭제
 *     · status snapshot / boot JSON 의 tof2 필드 삭제
 *   - 시작 트리거 = PyQt 후처리 작업자 페이지 "후처리 완료" 버튼 → 3 초 후 `start` 명령
 *     (POST /api/management/conveyor/CONV1/start → ExecuteCommand → CommandSubscriber
 *      → Serial "start\n" → handleCommand("start") → motorOn + ST_RUNNING).
 *   - 정지 트리거 = TOF1 (카메라 앞) 감지 → motorOff + ST_STOPPED + "STOPPED\n" 토큰.
 *     RUN_DURATION_MS=12000ms 는 *safety fallback* — TOF1 미감지 시 자동 정지로 cast 누락/
 *     센서 고장 보호. Cast 정상 운반 시 절대 fire 하지 않음 (TOF1 detect 가 먼저 일어남).
 *
 * 1.6.0 (2026-05-08): TOF1 위치 변경 (입구→카메라 앞 정지 트리거). 1.7.0 에서 TOF2 잔재 제거.
 * 1.5.1 (2026-04-22): RC522 카드 감지 실패 원인 제거 (주기 VersionReg healthcheck 삭제) +
 *                      SPEC-AMR-001 Wave 3 후처리존 핸드오프 버튼 (GPIO 33, INPUT_PULLUP).
 * 1.5.0 (2026-04-17): RC522 NDEF Text 레코드 파싱 + JSON 이벤트.
 *
 * v4.0 → v5.0 주요 변경점:
 *   1) WiFi/MQTT 기본 비활성 (#define ENABLE_WIFI_MQTT 0).
 *      Jetson Image Publisher 와 **USB Serial** (/dev/ttyUSB0 @ 115200) 로만 통신.
 *   2) ST_STOPPED 의 5초 타임아웃 자동 탈출 제거.
 *      → Jetson 의 "RUN\n" (또는 camera_ok/inspect_done) 수신 시에만 POST_RUN 진입.
 *   3) Serial 프로토콜 토큰:
 *        ESP → Jetson: BOOT / STOPPED / STARTED / DONE / PONG / STATE:<name>
 *        Jetson → ESP: start / RUN / STOP / PING / STATUS / camera_ok / inspect_done
 *
 * 핀 배선 (1.7.0):
 *   - TOF250 (Taidacent) x1 ASCII @ 9600: TOF1=GPIO32 (카메라 앞 정지 트리거)
 *   - Motor L298N: ENA=GPIO25 (PWM), IN1=GPIO26, IN2=GPIO27
 *   - Handoff button: GPIO33 (INPUT_PULLUP)
 *   - RC522 RFID: VSPI (SCK=18, MISO=19, MOSI=23, SS=5, RST=22)
 *   - Debounce (DEBOUNCE_MS=100ms), MIN_RUN_MS=1000ms (motor start 후 TOF1 false trigger 차단)
 *
 * State flow (1.7.0):
 *   IDLE → (Jetson "start\n" RX) → motorOn() → RUNNING + "STATE:RUNNING" + "STARTED" 토큰
 *        → (TOF1 감지 && MIN_RUN_MS 경과) → STOPPED + "STOPPED\n" TX  [정상 경로]
 *        → (RUN_DURATION_MS 경과 시 TOF1 미감지) → STOPPED + "STOPPED\n" [safety fallback]
 *        → (Jetson "RUN\n" RX) → POST_RUN + "STARTED\n" TX
 *        → (POST_RUN_MS=4000ms 경과) → CLEARING + "DONE\n" TX
 *        → (TOF1 clear) → IDLE
 */

#include <HardwareSerial.h>
#include <SPI.h>
#include <MFRC522.h>

// === Feature flags ===
#define ENABLE_WIFI_MQTT 0   // v5.0 기본 꺼둠. 1 로 바꾸면 v4.0 동작 (config.h 필요)

#if ENABLE_WIFI_MQTT
  #include <WiFi.h>
  #include "ESP32MQTTClient.h"
  #include "config.h"
  ESP32MQTTClient mqttClient;
  bool mqttConnected = false;
  unsigned long lastHeartbeat = 0;
#endif

// === Pin Definitions ===
static const int PIN_MOTOR_ENA = 25;
static const int PIN_MOTOR_IN1 = 26;
static const int PIN_MOTOR_IN2 = 27;
// 2026-04-29: PIN_TOF1_RX 16 → 32 변경 (원 GPIO16 라인 hardware 단선 우회)
// 2026-05-08: TOF1 센서 위치 입구 → 카메라 앞 이동 (배선 GPIO32 그대로). 정지 트리거.
// 2026-05-08 (1.7.0): TOF2 코드 완전 제거 — 핀 정의 / Serial 인스턴스 / globals 모두 삭제.
static const int PIN_TOF1_RX   = 32;

// === RFID-RC522 (VSPI 고정 배선, 2026-04-17 확정) ===
static const char* RFID_SPI_NAME = "VSPI";
static const uint8_t PIN_RFID_SCK  = 18;
static const uint8_t PIN_RFID_MISO = 19;
static const uint8_t PIN_RFID_MOSI = 23;
static const uint8_t PIN_RFID_SS   = 5;
static const uint8_t PIN_RFID_RST  = 22;

// === Handoff ACK Push Button (SPEC-AMR-001, 2026-04-17) ===
// 후처리존 A접점 푸시 버튼. 버튼 한쪽→GP33, 다른쪽→GND. INPUT_PULLUP 사용.
// 평시 HIGH, 눌림 LOW. release edge(LOW→HIGH) 에서 1회 이벤트 발행.
static const int PIN_HANDOFF_BUTTON = 33;
static const unsigned long HANDOFF_DEBOUNCE_MS = 50;   // 물리 디바운스
static const unsigned long HANDOFF_MIN_GAP_MS  = 500;  // 연타 병합 (동일 이벤트 처리)

// === Config ===
static const long TOF_BAUD = 9600;
// 2026-05-08 (1.7.0): TOF1 detect 범위 확장 (라이브 측정 결과 기반).
//   라이브 cast 운반 시 raw_mm 이 1~2 mm 사이 toggle 됨 → 종전 (MIN=2) 에선
//   raw_mm=1 인 시점에 미감지로 처리 → DEBOUNCE_MS=100ms 못 채워 STOPPED 트리거 누락 →
//   safety timeout (12 초) 까지 운행. MIN_MM=0 으로 낮춰 cast 가 sensor 에 가까워도
//   안정적 detect. MAX_MM 은 50→80 (sensor 마운트 거리 ~70mm 환경 수용 — 코멘트 정합).
static const int  DETECT_MIN_MM = 0;
static const int  DETECT_MAX_MM = 80;
static const int  MOTOR_SPEED   = 180;
static const unsigned long POST_RUN_MS     = 4000;  // v5: 사용자 요구 4초
static const unsigned long CLEAR_TIMEOUT_MS = 5000;
static const unsigned long REPORT_MS       = 300;
static const unsigned long DEBOUNCE_MS     = 100;  // 2026-05-06: 500→100ms 단축 (고속 통과 대응)
static const unsigned long MIN_RUN_MS      = 1000;
// 2026-05-08 (1.7.0): RUN_DURATION_MS 는 safety fallback 전용.
//   정상 경로 = "start" 명령 → 모터 ON → ST_RUNNING → TOF1 (카메라 앞) detect → STOPPED.
//   TOF1 미감지 시에도 RUN_DURATION_MS 후 자동 정지 → cast 누락 / 센서 고장 보호.
//   라이브 측정: cast 운반 시간 4~6 초 → safety margin 6 초 = 12 초 fallback.
//   Cast 가 정상 운반된다면 이 타임아웃은 절대 fire 하지 않음 (TOF1 detect 가 먼저).
static const unsigned long RUN_DURATION_MS = 12000;
static const unsigned long RFID_REINIT_MS        = 3000;   // 리더 lost 시 재초기화 간격
static const unsigned long RFID_HEALTHCHECK_MS   = 2000;
static const unsigned long RFID_TAG_HOLD_MS      = 700;

// v5: STOPPED 탈출은 Jetson RUN 수신에만 의존 (타임아웃 자동 탈출 제거)
// 안전 타임아웃이 필요하면 아래를 양수로 (0 = 무한 대기).
static const unsigned long STOP_WAIT_TIMEOUT_MS = 0;

// === States ===
enum State { ST_IDLE, ST_RUNNING, ST_STOPPED, ST_POST_RUN, ST_CLEARING };
const char* ST_NAME[] = {"IDLE", "RUNNING", "STOPPED", "POST_RUN", "CLEARING"};

// === Globals ===
HardwareSerial tof1(1);
MFRC522 rfid(PIN_RFID_SS, PIN_RFID_RST);  // v1.5.1: 생성자에서 핀 설정 (독립 스니펫과 동일)

State state = ST_IDLE;
unsigned long stateStart = 0;
unsigned long lastReport = 0;

int dist1 = -1;
bool det1 = false;
bool raw1 = false;
unsigned long det1Start = 0;
int objCount = 0;

// 2026-05-07 (sim sticky): sim_entry 가 readSensors 의 실 TOF1 덮어쓰기에 의해
// 즉시 무력화되는 문제 우회. 명령 수신 시 5 초간 raw1 강제 유지.
unsigned long simEntryUntilMs = 0;
static const unsigned long SIM_HOLD_MS = 5000;

bool visionResultReceived = false;
String visionResult = "";

String buf1 = "";

bool rfidReaderReady = false;
byte activeRfidVersion = 0;
unsigned long lastRfidInitAttemptMs = 0;
unsigned long lastRfidHealthcheckMs = 0;
unsigned long lastRfidSeenMs = 0;
String lastRfidUid = "";
String lastRfidType = "";

// Handoff button state
int  handoffLastRaw = HIGH;              // INPUT_PULLUP: 평시 HIGH
int  handoffStableLevel = HIGH;
unsigned long handoffLastChangeMs = 0;
unsigned long handoffLastEmitMs = 0;
unsigned long handoffPressCount = 0;

// NDEF Text payload (2026-04-17 추가)
#define RFID_TEXT_MAX_LEN 64
char lastRfidText[RFID_TEXT_MAX_LEN] = "";
int  lastRfidTextLen = 0;

// === Forward decls ===
void publishJson(const String& json);
void publishToken(const char* token);
void sendStatus();
void handleCommand(const String& cmd, const String& source);
bool initRfid();
void pollRfid();
void pollHandoffButton();
void emitHandoffAck(const char* reason);

// === Motor Control ===
void motorOn() {
  digitalWrite(PIN_MOTOR_IN1, HIGH);
  digitalWrite(PIN_MOTOR_IN2, LOW);
  ledcWrite(PIN_MOTOR_ENA, MOTOR_SPEED);
}
void motorOff() {
  digitalWrite(PIN_MOTOR_IN1, LOW);
  digitalWrite(PIN_MOTOR_IN2, LOW);
  ledcWrite(PIN_MOTOR_ENA, 0);
}

// === TOF250 ASCII Parser (v4.0 동일) ===
int parseTofLine(HardwareSerial& ss, String& buffer) {
  int result = -1;
  while (ss.available()) {
    char c = ss.read();
    if (c == '\r' || c == '\n') {
      if (buffer.length() > 0) {
        result = buffer.toInt();
        buffer = "";
      }
    } else if (isdigit(c)) {
      buffer += c;
      if (buffer.length() > 10) buffer = "";
    }
  }
  return result;
}

String uidToString(const MFRC522::Uid& uid) {
  String out;
  for (byte i = 0; i < uid.size; ++i) {
    if (i > 0) out += ':';
    if (uid.uidByte[i] < 0x10) out += '0';
    out += String(uid.uidByte[i], HEX);
  }
  out.toUpperCase();
  return out;
}

// === NDEF Text Record 파서 (NTAG213/215/216 MIFARE Ultralight 계열) ===
// 동작: 페이지 4~15(48 바이트) 에서 NDEF 메시지를 찾아 Text 레코드 페이로드만 추출.
// 설계 원칙: 블로킹 delay 금지, 버퍼 범위 엄격 검증, 실패 시 0 반환.
// 시그니처: out 버퍼는 null 종단됨. maxLen 은 '\0' 포함 크기.
static int parseNdefText(const byte *data, int dataLen, char *out, size_t maxLen) {
  if (maxLen == 0) return 0;
  out[0] = '\0';

  // NDEF TLV: 0x03 [len] 0xD1 [type_len=01] [payload_len] 'T' [status] [lang..] [text..]
  for (int i = 0; i < dataLen - 2; i++) {
    if (data[i] != 0x03 || data[i + 2] != 0xD1) continue;

    int idx = i + 2;                 // 0xD1 위치
    if (idx + 3 >= dataLen) break;
    idx++;                           // header(0xD1) 건너뜀
    idx++;                           // type_length 건너뜀 (보통 1)
    byte payloadLen = data[idx++];
    char type = (char)data[idx++];
    if (type != 'T') return 0;       // Text 레코드만 지원

    if (idx >= dataLen) return 0;
    byte status = data[idx++];
    byte langLen = status & 0x3F;    // 언어 코드 길이
    idx += langLen;                  // 언어 코드 건너뜀

    int textLen = (int)payloadLen - (int)langLen - 1;
    if (textLen <= 0 || idx + textLen > dataLen) return 0;
    if ((size_t)textLen >= maxLen) textLen = (int)maxLen - 1;

    for (int j = 0; j < textLen; j++) {
      out[j] = (char)data[idx + j];
    }
    out[textLen] = '\0';
    return textLen;
  }
  return 0;
}

// === 감지된 카드에서 NDEF Text 페이로드 읽기 ===
// 전제: PICC_IsNewCardPresent + PICC_ReadCardSerial 이 방금 성공해 세션이 열려 있음.
// MIFARE_Read 는 16 바이트씩(= 4 페이지) 반환. 3회 호출로 48 바이트 수집.
// 실패 시 지금까지 수집된 바이트만으로 파싱 시도 (짧은 NDEF 는 페이지 4~7 로 충분).
static int readNdefFromCard(char *out, size_t maxLen) {
  byte buffer[18];
  byte fullData[48];
  int offset = 0;

  for (byte page = 4; page < 16; page += 4) {
    byte bufSize = sizeof(buffer);
    MFRC522::StatusCode st = rfid.MIFARE_Read(page, buffer, &bufSize);
    if (st != MFRC522::STATUS_OK) break;
    for (int i = 0; i < 16 && offset < (int)sizeof(fullData); i++) {
      fullData[offset++] = buffer[i];
    }
  }
  return parseNdefText(fullData, offset, out, maxLen);
}

// === JSON 문자열 이스케이프 ===
// NDEF Text 가 따옴표나 백슬래시를 포함할 수 있으므로 publishJson 전에 적용.
// 제어 문자는 드롭 (CR/LF/TAB 만 escape sequence 유지).
static String jsonEscape(const char *s) {
  String r;
  if (s == nullptr) return r;
  size_t n = strlen(s);
  r.reserve(n + 4);
  for (size_t i = 0; i < n; ++i) {
    char c = s[i];
    if (c == '"' || c == '\\')      { r += '\\'; r += c; }
    else if (c == '\n')             r += "\\n";
    else if (c == '\r')             r += "\\r";
    else if (c == '\t')             r += "\\t";
    else if ((unsigned char)c < 0x20) { /* drop */ }
    else                            r += c;
  }
  return r;
}

void clearRfidTag() {
  if (lastRfidUid.length() == 0) return;

  publishJson(String("{\"event\":\"rfid_clear\",\"uid\":\"") + lastRfidUid + "\"}");
  lastRfidUid = "";
  lastRfidType = "";
  lastRfidText[0] = '\0';
  lastRfidTextLen = 0;
}

// RC522 를 VSPI 배선으로 1회 초기화. 실패 시 false 반환 (주기적 재시도).
// v1.5.1: 독립 스니펫과 동일하게 SPI.begin() 기본값 사용, PCD_Init 무인자 호출.
bool initRfid() {
  clearRfidTag();
  rfidReaderReady = false;
  activeRfidVersion = 0;
  lastRfidInitAttemptMs = millis();

  SPI.begin();     // ESP32 기본 VSPI: SCK=18, MISO=19, MOSI=23, SS=5 (고정 배선과 동일)
  rfid.PCD_Init(); // 생성자에 전달한 SS/RST 핀 사용
  delay(50);

  // 2026-05-07 (SPEC-RFID-001): 안테나 게인을 default(33dB) → max(48dB) 로 상향.
  // 이전 standalone 검증(Confluence 30539816)은 default 동작했으나 production 환경의
  // 모터/전원 노이즈 + 안테나 주변 metal 영향으로 인식 거리가 짧아질 수 있음.
  rfid.PCD_SetAntennaGain(MFRC522::RxGain_max);

  byte version = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  if (version == 0x00 || version == 0xFF) {
    publishJson("{\"event\":\"rfid_reader\",\"status\":\"not_found\"}");
    Serial.println("RFID:READER:NOT_FOUND");
    return false;
  }

  rfidReaderReady = true;
  activeRfidVersion = version;
  lastRfidHealthcheckMs = lastRfidInitAttemptMs;

  publishJson(
    String("{\"event\":\"rfid_reader\",\"status\":\"ready\",\"spi\":\"")
    + RFID_SPI_NAME
    + "\",\"ss\":" + PIN_RFID_SS
    + ",\"rst\":" + PIN_RFID_RST
    + ",\"version\":\"0x" + String(version, HEX) + "\"}"
  );
  Serial.printf(
    "RFID:READER:FOUND spi=%s sck=%u miso=%u mosi=%u ss=%u rst=%u version=0x%02X\n",
    RFID_SPI_NAME, PIN_RFID_SCK, PIN_RFID_MISO, PIN_RFID_MOSI,
    PIN_RFID_SS, PIN_RFID_RST, version
  );
  return true;
}

void pollRfid() {
  unsigned long now = millis();
  // 2026-05-07 (SPEC-RFID-001 디버그): polling/detection 카운터.
  // 1초마다 {"event":"rfid_dbg","poll":N,"detect":N} 출력 → main loop 실제 빈도 + 카드 인식 여부 진단.
  static unsigned long dbgLastMs = 0;
  static unsigned long dbgPollCount = 0;
  static unsigned long dbgPresentCount = 0;
  static unsigned long dbgReadCount = 0;
  dbgPollCount++;

  if (!rfidReaderReady) {
    if (now - lastRfidInitAttemptMs >= RFID_REINIT_MS) {
      initRfid();
    }
    return;
  }

  // v1.5.1: 주기 VersionReg healthcheck 제거 (카드 감지 간섭 의심).
  // RC522 가 PICC_IsNewCardPresent 자체로 상태 확인되므로 별도 헬스체크 불필요.
  // 필요 시 에러 누적 후 재초기화 로직 추후 추가.

  bool present = rfid.PICC_IsNewCardPresent();
  if (present) dbgPresentCount++;

  if (now - dbgLastMs >= 1000) {
    char dbgBuf[140];
    snprintf(
      dbgBuf, sizeof(dbgBuf),
      "{\"event\":\"rfid_dbg\",\"poll\":%lu,\"present\":%lu,\"read\":%lu,\"gain\":\"max\"}",
      dbgPollCount, dbgPresentCount, dbgReadCount
    );
    publishJson(dbgBuf);
    dbgLastMs = now;
    dbgPollCount = 0;
    dbgPresentCount = 0;
    dbgReadCount = 0;
  }

  if (!present || !rfid.PICC_ReadCardSerial()) {
    if (lastRfidUid.length() > 0 && now - lastRfidSeenMs >= RFID_TAG_HOLD_MS) {
      clearRfidTag();
    }
    return;
  }
  dbgReadCount++;

  String uid = uidToString(rfid.uid);
  MFRC522::PICC_Type cardType = rfid.PICC_GetType(rfid.uid.sak);
  String cardTypeName = String(rfid.PICC_GetTypeName(cardType));

  lastRfidSeenMs = now;
  if (uid != lastRfidUid) {
    lastRfidUid = uid;
    lastRfidType = cardTypeName;

    // NDEF Text 읽기 (실패해도 UID 이벤트는 항상 발행)
    char textBuf[RFID_TEXT_MAX_LEN];
    lastRfidTextLen = readNdefFromCard(textBuf, sizeof(textBuf));
    strncpy(lastRfidText, textBuf, sizeof(lastRfidText) - 1);
    lastRfidText[sizeof(lastRfidText) - 1] = '\0';

    String json = String("{\"event\":\"rfid_tag\",\"uid\":\"") + uid
                + "\",\"type\":\"" + cardTypeName + "\"";
    if (lastRfidTextLen > 0) {
      json += ",\"text\":\"" + jsonEscape(lastRfidText) + "\"";
    }
    json += "}";
    publishJson(json);

    Serial.print("RFID_UID:");
    Serial.println(uid);
    if (lastRfidTextLen > 0) {
      Serial.print("RFID_TEXT:");
      Serial.println(lastRfidText);
    }
  }

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}

// === Handoff ACK Button (SPEC-AMR-001) ===
// Token line: HANDOFF_ACK + JSON event. Jetson bridge 가 파싱해 Mgmt gRPC 로 전달.
// 연타/채터링 방지: 디바운스 50 ms + 연속 이벤트 500 ms 최소 간격.
void emitHandoffAck(const char* reason) {
  unsigned long now = millis();
  handoffPressCount++;
  handoffLastEmitMs = now;

  publishToken("HANDOFF_ACK");   // ★ Jetson bridge 가 주로 이 토큰 사용
  String json = String("{\"event\":\"handoff_ack\",\"zone\":\"postprocessing\"")
              + ",\"ts\":" + String(now)
              + ",\"count\":" + String(handoffPressCount)
              + ",\"reason\":\"" + reason + "\""
              + (strcmp(reason, "sim") == 0 ? ",\"sim\":true" : "")
              + "}";
  publishJson(json);
}

void pollHandoffButton() {
  unsigned long now = millis();
  int raw = digitalRead(PIN_HANDOFF_BUTTON);

  if (raw != handoffLastRaw) {
    handoffLastRaw = raw;
    handoffLastChangeMs = now;
    return;  // 아직 디바운스 대기
  }

  // 디바운스 기간 유지된 값 → stable 로 승격
  if (now - handoffLastChangeMs < HANDOFF_DEBOUNCE_MS) return;
  if (raw == handoffStableLevel) return;  // 상태 변화 없음

  int prev = handoffStableLevel;
  handoffStableLevel = raw;

  // rising edge: 눌렀다 뗀 순간 (LOW → HIGH)
  if (prev == LOW && raw == HIGH) {
    if (now - handoffLastEmitMs >= HANDOFF_MIN_GAP_MS) {
      emitHandoffAck("button");
    }
  }
}

// === Sensor Reading (TOF1 단일 센서, 1.7.0) ===
void readSensors() {
  unsigned long now = millis();

  int d1 = parseTofLine(tof1, buf1);
  if (d1 >= 0) {
    dist1 = d1;
    raw1 = (d1 >= DETECT_MIN_MM && d1 <= DETECT_MAX_MM);
  }

  // sim sticky: sim_entry 의 raw1 강제를 일정 시간 유지.
  if (now < simEntryUntilMs) { raw1 = true; if (dist1 < DETECT_MIN_MM) dist1 = 50; }

  if (raw1) {
    if (det1Start == 0) det1Start = now;
    if (now - det1Start >= DEBOUNCE_MS) det1 = true;
  } else {
    det1Start = 0;
    det1 = false;
  }
}

// === Event publish (Serial + optional MQTT) ===
void publishJson(const String& json) {
  Serial.println(json);
#if ENABLE_WIFI_MQTT
  if (mqttConnected) mqttClient.publish(TOPIC_EVENT, json.c_str(), 0, false);
#endif
}

// v5 신규: Jetson bridge 가 파싱하는 간단 토큰 라인
void publishToken(const char* token) {
  Serial.println(token);
}

void setState(State s) {
  String msg = String("{\"event\":\"state\",\"to\":\"") + ST_NAME[s] + "\"}";
  publishJson(msg);
  // v5: Jetson bridge 가 인식하는 STATE:<NAME> 라인 추가
  Serial.print("STATE:");
  Serial.println(ST_NAME[s]);
  state = s;
  stateStart = millis();
}

// === State Machine ===
void update() {
  unsigned long elapsed = millis() - stateStart;

  switch (state) {
    case ST_IDLE:
      // 2026-05-08: TOF1 입구 진입 트리거 폐기. ST_IDLE 에서는 외부 "start" 명령만 대기.
      // PyQt "후처리 완료" 버튼 → 3 초 카운트다운 → POST /api/management/conveyor/CONV1/start
      // → ExecuteCommand → CommandSubscriber → "start\n" Serial → handleCommand("start") →
      // motorOn() + setState(ST_RUNNING).
      break;

    case ST_RUNNING:
      // 2026-05-08: 정상 경로 = TOF1 (카메라 앞) detect → 모터 정지.
      // MIN_RUN_MS guard: motor on 직후 raw1 잔재 / debounce 미경과 false trigger 방어.
      // RUN_DURATION_MS guard: TOF1 미감지 시 안전 자동 정지 (cast 누락 / 센서 고장 보호).
      if (det1 && elapsed >= MIN_RUN_MS) {
        motorOff();
        visionResultReceived = false;
        visionResult = "";
        publishJson(String("{\"event\":\"exit\",\"trigger\":\"tof1_at_camera\","
                           "\"dist\":") + dist1 + ",\"elapsed_ms\":" + elapsed
                    + ",\"inspection\":\"requested\"}");
        setState(ST_STOPPED);
        publishToken("STOPPED");  // ★ Jetson bridge 트리거 → 캡처 + auto_run delay
      } else if (elapsed >= RUN_DURATION_MS) {
        motorOff();
        visionResultReceived = false;
        visionResult = "";
        publishJson(String("{\"event\":\"exit\",\"trigger\":\"safety_timeout\","
                           "\"elapsed_ms\":") + elapsed
                    + ",\"warn\":\"tof1_not_detected\","
                    + "\"inspection\":\"requested\"}");
        setState(ST_STOPPED);
        publishToken("STOPPED");
      }
      break;

    case ST_STOPPED:
      // v5: Jetson 의 RUN/camera_ok/inspect_done 수신 시에만 탈출
      if (visionResultReceived) {
        objCount++;
        motorOn();
        publishJson(String("{\"event\":\"post_start\",\"count\":") + objCount
                    + ",\"reason\":\"jetson_run\""
                    + ",\"result\":\"" + (visionResult.length() ? visionResult : "unknown") + "\"}");
        visionResultReceived = false;
        setState(ST_POST_RUN);
        publishToken("STARTED");  // ★ Jetson 에 모터 재시작 통지
      } else if (STOP_WAIT_TIMEOUT_MS > 0 && elapsed >= STOP_WAIT_TIMEOUT_MS) {
        // 안전 타임아웃 (기본 0 = 비활성)
        objCount++;
        motorOn();
        publishJson("{\"event\":\"post_start\",\"reason\":\"safety_timeout\"}");
        setState(ST_POST_RUN);
        publishToken("STARTED");
      }
      break;

    case ST_POST_RUN:
      if (elapsed >= POST_RUN_MS) {
        publishJson(String("{\"event\":\"post_done\",\"tof1_det\":")
                    + (det1 ? "true" : "false") + "}");
        publishToken("DONE");  // ★ 4초 구동 완료
        setState(ST_CLEARING);
      }
      break;

    case ST_CLEARING:
      // 2026-05-08 (1.7.0): TOF1 (카메라 앞) clear 까지 대기 — cast 가 카메라 앞을
      // 통과해야 IDLE 복귀. CLEAR_TIMEOUT_MS 는 sensor 상시 detect (cast stuck 등) 보호.
      if (!det1) {
        motorOff();
        publishJson(String("{\"event\":\"cycle_done\",\"count\":") + objCount + "}");
        raw1 = false;
        det1 = false;
        det1Start = 0;
        setState(ST_IDLE);
      } else if (elapsed >= CLEAR_TIMEOUT_MS) {
        motorOff();
        publishJson("{\"event\":\"clear_timeout\",\"warn\":\"tof1_stuck\"}");
        raw1 = false;
        det1 = false;
        det1Start = 0;
        setState(ST_IDLE);
      }
      break;
  }
}

// === Command handler ===
void handleCommand(const String& cmd, const String& source) {
  // v5 신규/기존 호환 alias
  if (cmd == "RUN" || cmd == "run" || cmd == "camera_ok" || cmd == "inspect_done") {
    visionResult = "ok";
    visionResultReceived = true;
    Serial.printf("{\"ack\":\"run\",\"src\":\"%s\"}\n", source.c_str());
  }
  else if (cmd == "PING" || cmd == "ping") {
    publishToken("PONG");
  }
  else if (cmd == "STATUS" || cmd == "status") {
    sendStatus();
  }
  else if (cmd == "STOP" || cmd == "stop") {
    // 1.5.2 (2026-04-29): STOP 명령에서 raw/det/detStart 동시 클리어.
    // 미클리어 시 sim_entry 잔재(raw1=true)로 인해 다음 update() 사이클에 즉시 ST_RUNNING 재진입.
    motorOff();
    raw1 = false;
    det1 = false;
    det1Start = 0;
    visionResultReceived = false;
    visionResult = "";
    setState(ST_IDLE);
  }
  else if (cmd == "RFID_SCAN" || cmd == "rfid_scan") {
    initRfid();
    Serial.println("{\"ack\":\"rfid_scan\"}");
  }
  else if (cmd == "sim_ack" || cmd == "SIM_ACK") {
    // SPEC-AMR-001 FR-AMR-01-07: HW 없이도 실제 버튼과 동일한 이벤트 발행
    emitHandoffAck("sim");
    Serial.println("{\"ack\":\"sim_ack\"}");
  }
  else if (cmd == "start") {
    // 2026-05-08 (1.7.0): PyQt "후처리 완료" 버튼 → 3 초 후 새 시작 트리거.
    // ST_RUNNING 진입 전에 TOF1 잔재 detect 클리어 — sim_entry sticky / 이전 cycle clear
    // 누락으로 인해 motor on 직후 즉시 ST_STOPPED 진입하는 false trigger 방어.
    raw1 = false;
    det1 = false;
    det1Start = 0;
    simEntryUntilMs = 0;
    visionResultReceived = false;
    visionResult = "";
    motorOn();
    setState(ST_RUNNING);
    publishToken("STARTED");  // ★ Jetson 에 모터 시작 통지 (state STATE:RUNNING 외 추가 신호)
  }
  else if (cmd == "reset") {
    motorOff();
    objCount = 0;
    raw1 = false;
    det1 = false;
    det1Start = 0;
    simEntryUntilMs = 0;
    visionResultReceived = false;
    visionResult = "";
    setState(ST_IDLE);
  }
  else if (cmd == "sim_entry") {
    // 1.7.0: sim_entry 는 ST_RUNNING 중 TOF1 detect 시뮬레이션 (정지 트리거 시뮬).
    // 1.5.x 시점 의 sim_exit 는 TOF2 시뮬이었으나 1.7.0 에서 삭제됨.
    raw1 = true; det1 = true; dist1 = 50;
    det1Start = millis() - DEBOUNCE_MS;
    simEntryUntilMs = millis() + SIM_HOLD_MS;
    Serial.println("{\"ack\":\"sim_entry\"}");
  }
  else if (cmd.length() > 0) {
    Serial.print("ERR:unknown_cmd:");
    Serial.println(cmd);
  }
}

// === Command parser (USB Serial only in v5) ===
String cmdBuf;
void processCmd() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdBuf.length() > 0) {
        cmdBuf.trim();
        handleCommand(cmdBuf, "usb");
        cmdBuf = "";
      }
    } else if (cmdBuf.length() < 64) {
      cmdBuf += c;
    } else {
      cmdBuf = "";
    }
  }
}

void sendStatus() {
  bool motorRunning = digitalRead(PIN_MOTOR_IN1) || digitalRead(PIN_MOTOR_IN2);
  char buf[640];
  snprintf(buf, sizeof(buf),
    "{\"state\":\"%s\",\"elapsed\":%lu,\"motor\":%s,"
    "\"range\":{\"min\":%d,\"max\":%d},"
    "\"tof1\":{\"mm\":%d,\"det\":%s,\"role\":\"camera_stop\"},"
    "\"rfid\":{\"ready\":%s,\"spi\":\"%s\",\"ss\":%u,"
    "\"rst\":%u,\"uid\":\"%s\",\"type\":\"%s\",\"text\":\"%s\",\"version\":\"0x%02X\"},"
    "\"count\":%d}",
    ST_NAME[state], millis() - stateStart,
    motorRunning ? "true" : "false",
    DETECT_MIN_MM, DETECT_MAX_MM,
    dist1, det1 ? "true" : "false",
    rfidReaderReady ? "true" : "false",
    RFID_SPI_NAME,
    PIN_RFID_SS,
    PIN_RFID_RST,
    lastRfidUid.c_str(),
    lastRfidType.c_str(),
    lastRfidText,    // NDEF Text (비어 있으면 빈 문자열)
    activeRfidVersion,
    objCount
  );
  Serial.println(buf);
}

// === Setup & Loop ===
void setup() {
  Serial.begin(115200);
  Serial.setTimeout(100);

  tof1.begin(TOF_BAUD, SERIAL_8N1, PIN_TOF1_RX, -1);

  pinMode(PIN_MOTOR_IN1, OUTPUT);
  pinMode(PIN_MOTOR_IN2, OUTPUT);
  digitalWrite(PIN_MOTOR_IN1, LOW);
  digitalWrite(PIN_MOTOR_IN2, LOW);
  ledcAttach(PIN_MOTOR_ENA, 1000, 8);
  ledcWrite(PIN_MOTOR_ENA, 0);

  // SPEC-AMR-001: 후처리존 핸드오프 버튼 (INPUT_PULLUP)
  pinMode(PIN_HANDOFF_BUTTON, INPUT_PULLUP);
  handoffLastRaw = digitalRead(PIN_HANDOFF_BUTTON);
  handoffStableLevel = handoffLastRaw;

  delay(500);
  Serial.println();
  Serial.println("BOOT:conveyor_v5_serial 1.7.0");
  publishJson("{\"boot\":\"conveyor_v5.0_1.7.0\","
              "\"tof1\":{\"pin\":32,\"role\":\"camera_stop\"},"
              "\"tof_baud\":9600,\"proto\":\"serial_only\","
              "\"start_trigger\":\"command:start\","
              "\"rfid\":{\"spi\":\"VSPI\",\"sck\":18,\"miso\":19,"
              "\"mosi\":23,\"ss\":5,\"rst\":22,\"ndef\":\"text\"},"
              "\"handoff\":{\"pin\":33,\"pull\":\"up\","
              "\"debounce_ms\":50,\"min_gap_ms\":500}}");
  setState(ST_IDLE);
  initRfid();

#if ENABLE_WIFI_MQTT
  // v4.0 스타일 WiFi/MQTT 는 필요 시 이 블록 활성화
#endif
}

void loop() {
  readSensors();
  update();
  processCmd();
  pollRfid();
  pollHandoffButton();

  if (millis() - lastReport >= REPORT_MS) {
    lastReport = millis();
    sendStatus();
  }
}
