# MinuteBooks

MinuteBooks helps teens master their finances by converting spending into hours of work.

## Backend quick start

```bash
python -m pip install -r requirements.txt
python app.py
```

The Flask backend initializes an SQLite database automatically and exposes starter API endpoints:

- `POST /register`, `POST /login`, `POST /logout`
- `GET/POST/PUT/DELETE /expenses`
- `GET/POST/PUT/DELETE /income`
- `GET/POST/PUT/DELETE /work-logs`
- `GET/POST/PUT/DELETE /budgets`
- `GET/POST/PUT/DELETE /categories`
- `GET/PUT /settings`
- `GET /analytics`
