#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_Fingerprint.h>
#include <SoftwareSerial.h>

LiquidCrystal_I2C lcd(0x27, 16, 2);
SoftwareSerial sim800(2, 3); 
SoftwareSerial fingerSerial(4, 5); 
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&fingerSerial);

void setup() {
  Serial.begin(9600); // Python se baat karne ke liye

  lcd.init(); 
  lcd.backlight();
  lcd.setCursor(0,0); 
  lcd.print("SmartEntry 2FA");
  delay(2000);

  sim800.begin(9600);
  finger.begin(57600);
  
  // 🛡️ Initial Sensor Connection Check
  fingerSerial.listen();
  if (finger.verifyPassword()) {
    lcd.clear(); 
    lcd.print("Sensor Found!");
  } else {
    lcd.clear(); 
    lcd.print("Sensor Error!");
    Serial.println("FINGERPRINT_NOT_FOUND");
    delay(3000);
  }
  delay(2000);

  lcd.clear(); 
  lcd.print("System Ready!");
  delay(1000);
  lcd.clear(); 
  lcd.print("Waiting Commands");
}

void loop() {
  // 1. Python se Command aane ka wait karo
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n'); // Poori line padho
    command.trim();

    // 🟢 COMMAND 1: Naya Fingerprint Register (e.g. "ENROLL,5")
    if (command.startsWith("ENROLL,")) {
      int id = command.substring(7).toInt();
      lcd.clear(); 
      lcd.print("Enrolling ID:"); 
      lcd.print(id);
      
      if (enrollFingerprint(id)) {
        Serial.println("ENROLL_SUCCESS"); // Python ko batao ho gaya
        lcd.clear(); 
        lcd.print("Saved Success!");
      } else {
        Serial.println("ENROLL_FAIL");
        lcd.clear(); 
        lcd.print("Enroll Failed!");
      }
      delay(2000);
      lcd.clear(); 
      lcd.print("Waiting Commands");
    }
    
    // 🔵 COMMAND 2: 2FA Verification (e.g. "VERIFY,5")
    else if (command.startsWith("VERIFY,")) {
      int expected_id = command.substring(7).toInt();
      lcd.clear(); 
      lcd.print("Place Finger...");
      
      int scanned_id = verifyFingerprint(10000); // 10 sec ka time do
      
      if (scanned_id == expected_id) {
        Serial.println("VERIFY_SUCCESS");
        lcd.clear(); 
        lcd.print("2FA Matched!");
      } else {
        Serial.println("VERIFY_FAIL");
        lcd.clear(); 
        lcd.print("Wrong Finger!");
      }
      delay(2000);
      lcd.clear(); 
      lcd.print("Waiting Commands");
    }
    
    // 🟠 COMMAND 3: SMS Bhejna (e.g. "SMS,9XXXXXXXXX,Alert message")
    else if (command.startsWith("SMS,")) {
      int firstComma = command.indexOf(',');
      int secondComma = command.indexOf(',', firstComma + 1);
      
      String number = command.substring(firstComma + 1, secondComma);
      String msg = command.substring(secondComma + 1);
      
      // 🛠️ SMART FIX: Agar number mein +91 nahi laga hai, toh automatic jod do
      if (!number.startsWith("+91")) {
        number = "+91" + number;
      }
      
      lcd.clear(); 
      lcd.print("Sending SMS...");
      sendSMS(number, msg);
      lcd.clear(); 
      lcd.print("Waiting Commands");
    }

    // 🛠️ NAYA COMMAND 4: HARDWARE DIAGNOSTICS TEST
    else if (command == "CHECK_HW") {
      lcd.clear(); 
      lcd.print("Testing HW...");
      
      // 1. Fingerprint Sensor Check karega
      fingerSerial.listen();
      bool f_ok = finger.verifyPassword();
      
      // 2. GSM Module Check karega (AT command bhej kar)
      sim800.listen();
      sim800.println("AT");
      delay(200);
      bool gsm_ok = false;
      while (sim800.available()) {
        String res = sim800.readString();
        if (res.indexOf("OK") != -1) {
          gsm_ok = true;
        }
      }
      
      // 3. Final Report Python ko wapas bhejega
      if (f_ok && gsm_ok) {
        Serial.println("HW_STATUS:ALL_OK");
      } else if (!f_ok && gsm_ok) {
        Serial.println("HW_STATUS:FINGER_FAIL");
      } else if (f_ok && !gsm_ok) {
        Serial.println("HW_STATUS:GSM_FAIL");
      } else {
        Serial.println("HW_STATUS:ALL_FAIL");
      }
      
      delay(1000);
      lcd.clear(); 
      lcd.print("Waiting Commands");
    }
  }
}

// ==========================================
// 👆 FINGERPRINT ENROLLMENT FUNCTION
// ==========================================
bool enrollFingerprint(uint8_t id) {
  fingerSerial.listen();
  int p = -1;
  while (p != FINGERPRINT_OK) { p = finger.getImage(); }
  p = finger.image2Tz(1);
  if (p != FINGERPRINT_OK) return false;
  
  lcd.setCursor(0,1); 
  lcd.print("Remove Finger");
  delay(2000);
  while (finger.getImage() != FINGERPRINT_NOFINGER);
  
  lcd.setCursor(0,1); 
  lcd.print("Place Again  ");
  p = -1;
  while (p != FINGERPRINT_OK) { p = finger.getImage(); }
  p = finger.image2Tz(2);
  if (p != FINGERPRINT_OK) return false;
  
  p = finger.createModel();
  if (p != FINGERPRINT_OK) return false;
  
  p = finger.storeModel(id);
  if (p == FINGERPRINT_OK) return true;
  return false;
}

// ==========================================
// 👆 FINGERPRINT VERIFY FUNCTION
// ==========================================
int verifyFingerprint(long timeout_ms) {
  fingerSerial.listen();
  long startTime = millis();
  while (millis() - startTime < timeout_ms) {
    uint8_t p = finger.getImage();
    if (p == FINGERPRINT_OK) {
      p = finger.image2Tz();
      if (p == FINGERPRINT_OK) {
        p = finger.fingerFastSearch();
        if (p == FINGERPRINT_OK) return finger.fingerID; // Match
        else return -1; // Wrong finger
      }
    }
  }
  return -2; // Timeout
}

// ==========================================
// 📡 SMS FUNCTION
// ==========================================
void sendSMS(String number, String message) {
  sim800.listen();
  sim800.println("AT+CMGF=1"); 
  delay(500);
  sim800.print("AT+CMGS=\""); 
  sim800.print(number); 
  sim800.println("\""); 
  delay(500);
  sim800.print(message);
  delay(100);
  sim800.write(26); // Ctrl+Z key SMS bhejne ke liye
  delay(4000);
}