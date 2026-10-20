## Weather — open-meteo (NEVER a cron job!)

"Milyen idő lesz / hány fok X napon" → NOT web_search and NEVER a cron job/reminder — a forecast lookup NOW. `python_exec` + **open-meteo** (no key, 7-16 days):
1. Geocode: `geocoding-api.open-meteo.com/v1/search?name=<city>&count=1` → lat/lon.
2. `api.open-meteo.com/v1/forecast?latitude=&longitude=&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max&hourly=temperature_2m,precipitation_probability&timezone=auto&forecast_days=7`.
3. 🚨 COMPUTE the target date with `datetime` from today's date in the prompt (e.g. Tuesday→Thursday = +2 days) and index EXACTLY that day in `daily.time[]`/`hourly.time[]`, NOT today. "Este" = 18-22h hourly.
4. WMO codes: 0-3 clear/cloudy, 45-48 fog, 51-67 rain, 71-77 snow, 80-82 showers, 95-99 thunderstorm. ONE call = the whole week.
