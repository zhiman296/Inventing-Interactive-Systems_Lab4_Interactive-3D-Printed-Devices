const int PIN_LEFT  = 34;
const int PIN_RIGHT = 35;

int readAverage(int pin) {
  analogRead(pin);              // throw-away reading
  delayMicroseconds(200);       // let the ADC settle
  long sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += analogRead(pin);
    delayMicroseconds(100);
  }
  return sum / 16;
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);                       // values from 0 to 4095
  analogSetPinAttenuation(PIN_LEFT,  ADC_11db);   // full 0 - 3.3 V range
  analogSetPinAttenuation(PIN_RIGHT, ADC_11db);
}

void loop() {
  int left  = readAverage(PIN_LEFT);
  int right = readAverage(PIN_RIGHT);

  Serial.print(left);
  Serial.print(",");
  Serial.println(right);

  delay(20);
}
