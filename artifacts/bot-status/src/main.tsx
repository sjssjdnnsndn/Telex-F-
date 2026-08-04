import { createRoot } from 'react-dom/client';
import { setExtraHeadersGetter } from '@workspace/api-client-react';

import App from './App';

import './index.css';

// Attach the Telegram WebApp's signed `initData` to every API request so the
// backend can verify who is actually calling (see telegramAuth.ts on the
// server) instead of trusting a client-supplied user id.
setExtraHeadersGetter(() => {
  const initData = (window as any).Telegram?.WebApp?.initData;
  return initData ? { 'x-telegram-init-data': initData } : null;
});

createRoot(document.getElementById('root')!).render(<App />);
