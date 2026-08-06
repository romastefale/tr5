import express from 'express';
import dotenv from 'dotenv';

dotenv.config();

const app = express();
app.use(express.json());

const PORT = 3000;
const HOST = '0.0.0.0';

let _database_status = 'ready';
let _telegram_status = 'ready';
let _configured = true;

app.get('/healthz', (req, res) => {
  res.json({
    status: 'ok',
    commands: 9,
    rate_limit: { status: 'mock' }
  });
});

app.get('/readyz', (req, res) => {
  const ready = _database_status === 'ready' && _telegram_status === 'ready' && _configured;
  res.status(ready ? 200 : 503).json({
    status: ready ? 'ready' : 'not_ready',
    database: _database_status,
    telegram: _telegram_status,
  });
});

app.post('/webhook', (req, res) => {
  // Mock webhook response
  if (!_configured) {
    return res.status(503).send();
  }
  
  res.json({ ok: true });
});

app.listen(PORT, HOST, () => {
  console.log(`Server is running on http://${HOST}:${PORT}`);
});
