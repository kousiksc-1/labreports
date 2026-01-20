const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000';

export async function signup(email: string, password: string) {
  const r = await fetch(`${API_BASE}/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password })
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function login(email: string, password: string) {
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password })
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function startSession(token: string, title?: string) {
  const r = await fetch(`${API_BASE}/chat/start`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`
    },
    body: JSON.stringify({ title })
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function sendMessage(token: string, sessionId: string, message: string, fileId?: string) {
  const r = await fetch(`${API_BASE}/chat/send`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`
    },
    body: JSON.stringify({ session_id: sessionId, message, file_id: fileId })
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{ answer: string, intent?: string, patient_id?: string, patient_name?: string, document_parsed?: boolean }>;
}

export async function listSessions(token: string) {
  const r = await fetch(`${API_BASE}/chat/sessions`, {
    headers: { 'Authorization': `Bearer ${token}` }
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getMessages(token: string, sessionId: string) {
  const r = await fetch(`${API_BASE}/chat/messages?session_id=${encodeURIComponent(sessionId)}`, {
    headers: { 'Authorization': `Bearer ${token}` }
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function uploadFile(token: string, file: File, sessionId?: string) {
  const formData = new FormData();
  formData.append('file', file);
  if (sessionId) {
    formData.append('session_id', sessionId);
  }
  
  const r = await fetch(`${API_BASE}/chat/upload`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`
    },
    body: formData
  });
  
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{ 
    file_id: string, 
    filename: string, 
    file_path: string,
    content_type: string 
  }>;
}


