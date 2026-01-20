import React, { useEffect, useRef, useState } from 'react'
import { signup, login, startSession, sendMessage, listSessions, getMessages, uploadFile } from './api'
import './styles.css'

type ChatTurn = { role: 'user' | 'assistant' | 'system', content: string }
type Mode = 'login' | 'signup' | 'chat'

function LoginView({ onLogin, goSignup }: { onLogin: (email: string, password: string) => void, goSignup: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  return (
    <div style={{ maxWidth: 420, margin: '64px auto', display: 'grid', gap: 8 }}>
      <h2>Lab reports chat bot - Login</h2>
      <input placeholder="email" value={email} onChange={e => setEmail(e.target.value)} />
      <input placeholder="password" type="password" value={password} onChange={e => setPassword(e.target.value)} />
      <button onClick={() => onLogin(email, password)}>Log in</button>
      <button onClick={goSignup} style={{ background: 'transparent', border: 'none', color: '#0a58ca', textDecoration: 'underline', cursor: 'pointer' }}>Create an account</button>
    </div>
  )
}

function SignupView({ onSignup, goLogin }: { onSignup: (email: string, password: string) => void, goLogin: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  return (
    <div style={{ maxWidth: 420, margin: '64px auto', display: 'grid', gap: 8 }}>
      <h2>RAG Lab Chat - Signup</h2>
      <input placeholder="email" value={email} onChange={e => setEmail(e.target.value)} />
      <input placeholder="password" type="password" value={password} onChange={e => setPassword(e.target.value)} />
      <button onClick={() => onSignup(email, password)}>Sign up</button>
      <button onClick={goLogin} style={{ background: 'transparent', border: 'none', color: '#0a58ca', textDecoration: 'underline', cursor: 'pointer' }}>Back to login</button>
    </div>
  )
}

export default function App() {
  const [mode, setMode] = useState<Mode>('login')
  const [token, setToken] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [sessions, setSessions] = useState<{id:string,title:string,updated_at:string}[]>([])
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [title, setTitle] = useState('Lab Chat')
  const [input, setInput] = useState('')
  const [msgs, setMsgs] = useState<ChatTurn[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploadedDoc, setUploadedDoc] = useState<{ filename: string, fileId: string, contentType: string } | null>(null)
  const [uploading, setUploading] = useState(false)
  const chatRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (chatRef.current) {
      chatRef.current.scrollTop = chatRef.current.scrollHeight
    }
  }, [msgs])

  async function doSignup(email: string, password: string) {
    setError(null)
    try {
      const r = await signup(email, password)
      setToken(r.token)
      setMode('chat')
    } catch (e: any) {
      setError(e.message || 'Signup failed')
    }
  }

  async function doLogin(email: string, password: string) {
    setError(null)
    try {
      const r = await login(email, password)
      setToken(r.token)
      setMode('chat')
    } catch (e: any) {
      setError(e.message || 'Login failed')
    }
  }

  // Load sessions & auto-select/create one after entering chat mode
  useEffect(() => {
    if (mode !== 'chat' || !token) return
    (async () => {
      try {
        const s = await listSessions(token)
        setSessions(s)
        if (s.length > 0) {
          // select latest
          const sid = s[0].id
          setSessionId(sid)
          const history = await getMessages(token, sid)
          const converted: ChatTurn[] = history.map((m: any) => ({ role: m.role, content: m.content }))
          setMsgs(converted)
        } else {
          // create first session automatically
          const r = await startSession(token, title)
          setSessionId(r.session_id)
          setMsgs([])
          const s2 = await listSessions(token)
          setSessions(s2)
        }
      } catch (e: any) {
        setError(e.message || 'Failed to load sessions')
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, token])

  async function selectSession(id: string) {
    if (!token) return
    setSessionId(id)
    setUploadedDoc(null) // Clear uploaded document when switching sessions
    try {
      const history = await getMessages(token, id)
      const converted: ChatTurn[] = history.map((m: any) => ({ role: m.role, content: m.content }))
      setMsgs(converted)
    } catch (e: any) {
      setError(e.message || 'Failed to load messages')
    }
  }

  async function newChat() {
    if (!token) return
    setUploadedDoc(null) // Clear uploaded document when starting new chat
    try {
      const r = await startSession(token, '')
      setSessionId(r.session_id)
      setMsgs([])
      const s = await listSessions(token)
      setSessions(s)
    } catch (e: any) {
      setError(e.message || 'Failed to start chat')
    }
  }

  async function onStart() {
    if (!token) return
    setError(null)
    try {
      const r = await startSession(token, title)
      setSessionId(r.session_id)
      setMsgs([])
    } catch (e: any) {
      setError(e.message || 'Start failed')
    }
  }

  async function handleFileUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file || !token || !sessionId) return
    
    setUploading(true)
    setError(null)
    
    try {
      const result = await uploadFile(token, file, sessionId)
      
      // Store file info (not parsed content yet)
      setUploadedDoc({
        filename: result.filename,
        fileId: result.file_id,
        contentType: result.content_type
      })
      
      // Add simple system message
      setMsgs(m => [...m, {
        role: 'system',
        content: `📎 File attached: ${result.filename} (${result.content_type.toUpperCase()})\n\nFile will be processed when you send your question.`
      }])
      
      setError(null)
    } catch (e: any) {
      setError(e.message || 'Upload failed')
      setMsgs(m => [...m, {
        role: 'system',
        content: `❌ Upload failed: ${e.message || 'Unknown error'}`
      }])
    } finally {
      setUploading(false)
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    }
  }

  async function onSend(customText?: string) {
    const text = (customText ?? input).trim()
    if (!token || !sessionId || !text) return
    setBusy(true)
    setError(null)
    
    const hasDocument = !!uploadedDoc
    const fileId = uploadedDoc?.fileId
    
    // push user message and a placeholder assistant bubble for step-by-step feedback
    const initialMessage = hasDocument ? '⚙️ Processing document…' : 'Processing…'
    setMsgs(m => [...m, { role: 'user', content: text }, { role: 'assistant', content: initialMessage }])
    if (!customText) setInput('')
    
    try {
      // If document attached, show parsing step first
      if (hasDocument) {
        await new Promise(resolve => setTimeout(resolve, 400))
        setMsgs(m => {
          const copy = m.slice()
          copy[copy.length - 1] = { 
            role: 'assistant', 
            content: `📄 Parsing document: ${uploadedDoc.filename}\n⚙️ Extracting content...`
          }
          return copy
        })
      }
      
      // Send message with file_id if document attached
      const r = await sendMessage(token, sessionId, text, fileId)
      
      // Build step-by-step messages
      const steps: string[] = []
      
      // Show document parsed status if applicable
      if (hasDocument && r.document_parsed) {
        steps.push(`✅ Document parsed successfully`)
      }
      
      if (r.patient_name) {
        steps.push(`👤 Patient detected: ${r.patient_name}`)
      }
      if (r.intent) {
        const intentLabel = r.intent.toLowerCase() === 'md' ? 'Document' : r.intent.toUpperCase()
        steps.push(`🎯 Intent: ${intentLabel}`)
      }
      
      // Step 1: Show first status (document parsed or patient detection)
      if (steps.length > 0) {
        setMsgs(m => {
          const copy = m.slice()
          copy[copy.length - 1] = { role: 'assistant', content: steps[0] }
          return copy
        })
        await new Promise(resolve => setTimeout(resolve, 600))
      }
      
      // Step 2: Show accumulated steps
      if (steps.length > 1) {
        setMsgs(m => {
          const copy = m.slice()
          copy[copy.length - 1] = { 
            role: 'assistant', 
            content: steps.slice(0, 2).join('\n')
          }
          return copy
        })
        await new Promise(resolve => setTimeout(resolve, 600))
      }
      
      // Step 3: Show all steps if 3+
      if (steps.length > 2) {
        setMsgs(m => {
          const copy = m.slice()
          copy[copy.length - 1] = { 
            role: 'assistant', 
            content: steps.join('\n')
          }
          return copy
        })
        await new Promise(resolve => setTimeout(resolve, 600))
      }
      
      // Step 4: Show "generating answer" before final answer
      if (steps.length > 0) {
        setMsgs(m => {
          const copy = m.slice()
          const combinedSteps = steps.join('\n')
          copy[copy.length - 1] = { 
            role: 'assistant', 
            content: `${combinedSteps}\n\n⏳ Generating answer...`
          }
          return copy
        })
        await new Promise(resolve => setTimeout(resolve, 400))
      }
      
      // Final: Show the actual answer
      setMsgs(m => {
        const copy = m.slice()
        copy[copy.length - 1] = { role: 'assistant', content: r.answer }
        return copy
      })
      
    } catch (e: any) {
      setMsgs(m => {
        const copy = m.slice()
        copy[copy.length - 1] = { 
          role: 'assistant', 
          content: `❌ Error: ${e.message || 'Failed to process request'}`
        }
        return copy
      })
      setError(e.message || 'Send failed')
    } finally {
      setBusy(false)
    }
  }

  function logout() {
    setToken(null)
    setSessionId(null)
    setMsgs([])
    setMode('login')
  }

  if (mode === 'login') {
    return <LoginView onLogin={doLogin} goSignup={() => setMode('signup')} />
  }
  if (mode === 'signup') {
    return <SignupView onSignup={doSignup} goLogin={() => setMode('login')} />
  }

  // Chat
  return (
    <div className="container">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button className="hamburger" aria-label="Toggle sidebar" onClick={() => setSidebarOpen(s => !s)}>☰</button>
          <h2 className="page-title">Lab reports chatbot</h2>
        </div>
        <button onClick={logout}>Log out</button>
      </div>
      <div className={`cols`}>
        <div className={`card sidebar ${sidebarOpen ? 'open' : 'closed'}`}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <h3>Chats</h3>
            <button onClick={newChat}>New chat</button>
          </div>
          <div className="chat-list">
            {sessions.map(s => (
              <button key={s.id} className="chat-item" onClick={() => selectSession(s.id)}>
                {s.title || 'New chat'}
              </button>
            ))}
            {sessions.length === 0 && <div className="muted">No chats yet.</div>}
          </div>
        </div>
        <div className={`card chat-main ${sidebarOpen ? 'shifted' : ''}`}>
          <div className="chat-box" ref={chatRef}>
            {msgs.length === 0 ? (
              <div className="hero">
                <div className="hero-title">Lab reports query bot</div>
                <div className="hero-sub">Ask any information about lab reports</div>
                <div className="hero-grid">
                  <button className="hero-card" onClick={() => onSend("How many total patients showed up in 2024")}>
                    How many total patients showed up in 2024
                  </button>
                  <button className="hero-card" onClick={() => onSend("show me the creatinine levels of Anthony Wilson in the recent CMP test")}>
                    Show me the creatinine levels of Anthony Wilson in the recent CMP test
                  </button>
                  <button className="hero-card" onClick={() => onSend("show me the trend of T3 levels of Lewis Mark")}>
                    Show me the trend of T3 levels of Lewis Mark
                  </button>
                  <button className="hero-card" onClick={() => onSend("I seem to have high blood pressure, what should I do")}>
                    I seem to have high blood pressure, what should I do
                  </button>
                </div>
              </div>
            ) : (
              <>
                {msgs.map((m, i) => (
                  <div key={i} className="msg">
                    {m.role !== 'system' && (
                      <div className="msg-label">{m.role === 'user' ? 'You' : 'Assistant'}</div>
                    )}
                    <div className={`bubble ${m.role}`}>{m.content}</div>
                  </div>
                ))}
              </>
            )}
          </div>
          <div className="row" style={{ marginTop: 8, gap: 8 }}>
            <input
              type="file"
              ref={fileInputRef}
              style={{ display: 'none' }}
              accept=".pdf,.png,.jpg,.jpeg,.bmp,.tiff,.tif"
              onChange={handleFileUpload}
            />
            <button
              disabled={!sessionId || uploading || busy}
              onClick={() => fileInputRef.current?.click()}
              className="upload-btn"
              title="Upload PDF or image"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/>
              </svg>
            </button>
            <input
              style={{ flex: 1 }}
              placeholder="Ask about lab reports..."
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && !busy && !uploading ? onSend() : undefined}
              disabled={busy || uploading}
            />
            <button disabled={!sessionId || busy || uploading} onClick={() => onSend()}>
              {busy ? 'Sending...' : uploading ? 'Uploading...' : 'Send'}
            </button>
          </div>
          {uploadedDoc && (
            <div className="doc-banner">
              <span>📄 Document loaded: <strong>{uploadedDoc.filename}</strong></span>
              <button onClick={() => setUploadedDoc(null)} title="Clear document">✕</button>
            </div>
          )}
          {error && <div className="error" style={{ marginTop: 8 }}>{error}</div>}
        </div>
      </div>
    </div>
  )
}


