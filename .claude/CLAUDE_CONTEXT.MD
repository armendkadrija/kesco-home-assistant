# Home Assistant Project — Claude Code Context

## What This Project Is
A Home Assistant (HA) instance at `https://ha.sporchi.cloud` (HA 2026.3.4).
The user is **Armend Kadrija**, based in Kosovo, Albanian-speaking.
This repo holds custom integrations and components to be deployed to HA at `/config/`.

---

## Project Structure

```
home-assistant/
└── custom_components/
    └── kesco/          ← KESCO Energy custom integration (needs deploying)
        ├── __init__.py
        ├── api.py
        ├── config_flow.py
        ├── coordinator.py
        ├── const.py
        ├── manifest.json
        ├── sensor.py
        ├── strings.json
        └── translations/
            └── en.json
```

---

## KESCO Integration

### What It Does
Fetches electricity invoices from the KESCO consumer portal (kesco-energy.com)
and exposes them as Home Assistant sensor entities.

### Auth Flow
- Login endpoint: POST https://www.kesco-energy.com/api/TokenAuth
- Body: { "Username": "...", "Password": "...", "RememberMe": true }
- Response: { "State": 1, "Data": { "accessToken": "eyJ..." } }
- Token TTL: 7 days (604800 seconds) — no refresh token, just re-login
- No CAPTCHA on /api/TokenAuth (only on /api/TokenAuth/login)
- Credentials stored securely in HA config entry (.storage/core.config_entries)

### Invoice API
GET https://fatura.kesco-energy.com/api/Account/debitor-invoices/
    ?agencyId=DFE&debitorId=<debitorId>
Authorization: Bearer <token>
User-Agent: required (returns empty 200 without it)

### Invoice Response Structure (real API)
{
  "State": 1,
  "Msg": null,
  "Data": {
    "invoices": [ ... ],
    "lastUpdate": "2026-03-24T21:55:11+01:00"
  }
}

### Invoice Object Fields
{
  "InvInsOrder": 23388969,
  "BillingPeriodID": 2,          // month number (1=Jan, 2=Feb...)
  "BillingPeriodYear": 2026,
  "SeasonID": 1,                  // 1=Winter, 2=Summer
  "ActiveHighConsum": 337.0,      // A1 (High/Day) kWh
  "ActiveLowConsum": 13073.0,     // A2 (Low/Night) kWh  (cumulative meter reading!)
  "TotalConsum": 446.0,
  "BillAmount": 40.01,
  "ReadingDate": "2026-02-25T00:00:00",
  "DueDate": "2026-03-12T00:00:00",
  "ReferenceNumber": "509032232260200P"
}

### Sensor Entities Created After Setup
sensor.kesco_latest_bill_amount         EUR
sensor.kesco_latest_bill_kwh            kWh
sensor.kesco_latest_bill_a1_kwh         kWh (day)
sensor.kesco_latest_bill_a2_kwh         kWh (night)
sensor.kesco_latest_reading_date        date
sensor.kesco_latest_due_date            date
sensor.kesco_latest_period              "Feb 2026"
sensor.kesco_latest_reference           ref number
sensor.kesco_previous_bill_amount       EUR
sensor.kesco_previous_bill_kwh          kWh
sensor.kesco_previous_reading_date      date
sensor.kesco_previous_period            "Jan 2026"
sensor.kesco_total_billed_all_time      EUR
sensor.kesco_total_kwh_all_time         kWh
sensor.kesco_invoice_count              count

### Deployment
1. Copy custom_components/kesco/ to /config/custom_components/kesco/ on HA
2. Restart HA
3. Settings → Devices & Services → Add Integration → "KESCO Energy"
4. Enter: username, password, Agency ID = DPR, Debitor ID = 90189391
HA has Studio Code Server installed — use it for file transfer.

---

## HA Dashboard — Energy Cost

Dashboard path: energy-cost at https://ha.sporchi.cloud/energy-cost

### Custom JS Cards (inline HA dashboard resources)
Resource ID                          Card tag                    Purpose
3837b3bfc9fd4b13a445266c0f48d6f6    kosovo-range-card           Energy range analysis
807b9c920a8340aabef7517e19b173d0    kosovo-est-bill-card        Estimated monthly bill
537fc02b7f2248b2a7edada575fbc2aa    kosovo-device-cost-card     Device costs
47916b2045e34247b41be89ebe4bd919    kesco-invoice-card          Invoice history

### Key HA Entities
sensor.energy_meter_main_produced_energy  MAIN meter — INVERTED, produced_energy = actual consumed
sensor.energy_meter_main_power            Live W — NEGATIVE, use abs()
sensor.energy_meter_main_energy_produced_a/b/c  Phase kWh
sensor.energy_meter_main_power_b/c        Phase power W (negative)
sensor.energy_meter_main_voltage_a/b/c    Phase voltages
sensor.energy_meter_main_ac_frequency     Grid Hz
sensor.refrigerator_energy / _power       Fridge smart plug
sensor.65_oled_energy / _power            TV smart plug (often unavailable)

---

## Kosovo Electricity Tariff — Group 4/02
Source: Real KESCO bill 02-2026. VAT = 8%, Fixed = 2.32 EUR/month.

Block 1 (<=800 kWh/mo):  A1 Day = 0.0905 EUR/kWh,  A2 Night = 0.0388 EUR/kWh
Block 3 (>800 kWh/mo):   A1 Day = 0.1543 EUR/kWh,  A2 Night = 0.0728 EUR/kWh

Time-of-use (Belgrade timezone):
  Winter Oct-Mar:  A1 (High) = 07:00-22:00 local (UTC+1)
  Summer Apr-Sep:  A1 (High) = 08:00-23:00 local (UTC+2)

IMPORTANT — Meter is physically inverted (CT clamp reversed):
  - Real consumption = sensor.energy_meter_main_produced_energy
  - Power readings are negative — always abs()

Billing periods use actual meter reading dates, NOT calendar month ends:
  Feb 2026: Jan 26 to Feb 24 (1161 kWh, EUR 115.04 official bill)
  Jan 2026: Dec 26 to Jan 26 (1392 kWh, EUR 147.16)

---

## HA Instance Details
URL:      https://ha.sporchi.cloud
Version:  2026.3.4
Timezone: Europe/Belgrade (UTC+1 winter, UTC+2 summer)
Entities: ~307 across 22 domains

Notable: Zigbee2MQTT, Xiaomi Air Purifier, Gree AC (Klima/Nxemsja),
         Samsung SmartThings (TV + fridge), MS Energy Scooter, HACS

HACS cards installed: button-card, mushroom, Bubble-Card, mini-graph-card

Helpers created (can delete after integration deployed):
  input_text.kesco_username
  input_text.kesco_password
  input_text.kesco_token
  input_datetime.kesco_token_expiry

---

## Energy Calculation Logic (used in custom cards)

Cards use hass.callWS() — NOT raw fetch:

  const result = await hass.callWS({
    type: 'recorder/statistics_during_period',
    start_time: from, end_time: to,
    statistic_ids: ['sensor.energy_meter_main_produced_energy'],
    period: 'hour',
    types: ['change']
  });
  // rows[n] = { start: utcMs, change: kWhInThatHour }

Per-hour classification:
  1. Convert UTC to Belgrade local (UTC+1 winter, UTC+2 summer)
  2. Check if hour is A1 (day) or A2 (night)
  3. Track running kWh total — split hour proportionally at 800 kWh boundary
  4. Result: 4 buckets B1A1, B1A2, B3A1, B3A2

Real Feb 2026 result:
  B1 A1: 530.6 kWh (66.3%) x 0.0905 = 48.02 EUR
  B1 A2: 269.4 kWh (33.7%) x 0.0388 = 10.45 EUR
  B3 A1: 200.3 kWh (63.7%) x 0.1543 = 30.91 EUR
  B3 A2: 113.9 kWh (36.3%) x 0.0728 =  8.29 EUR
  Fixed: 2.32 → Neto 100.00 → +8% VAT → 108.00 EUR
  (vs 115.04 EUR official — ~7 EUR gap from CT clamp calibration drift)

---

## What Still Needs Doing
1. Deploy KESCO integration to HA (main task)
2. After deploy: update kosovo-range-card to use sensor.kesco_latest_reading_date
   as current period start (instead of fetching invoice API from browser)
3. After deploy: update kesco-invoice-card to read sensor.kesco_* entities
   instead of fetching the invoice API directly
4. Delete the 4 helper entities listed above