// First-upload smoke test for the Wemos D1 mini: blinks the on-board LED
// (D4/GPIO2, active low) and prints chip info over serial at 115200 baud.
void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  delay(200);
  Serial.printf("\nco2watch hello: chip %08X, flash %u KB, %u MHz\n",
                ESP.getChipId(), ESP.getFlashChipRealSize() / 1024, ESP.getCpuFreqMHz());
}

void loop() {
  digitalWrite(LED_BUILTIN, LOW);   // on
  delay(200);
  digitalWrite(LED_BUILTIN, HIGH);  // off
  delay(800);
  Serial.printf("uptime %lu s\n", millis() / 1000);
}
