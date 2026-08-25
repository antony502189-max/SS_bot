import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { useForm } from 'react-hook-form'
import './styles.css'

const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api/v1'
const client = new QueryClient()
type User = { id: string; full_name: string | null; telegram_username: string | null; role: 'participant' | 'sector_head' | 'admin' }
type Task = { id: string; title: string; description: string | null; status: string; deadline: string; kind: string }
type Item = { id: string; title: string; is_completed: boolean }
type Detail = Task & { checklist: Item[]; members: { user: User; is_creator: boolean; is_leader: boolean }[] }
type Chat = { status: string; telegram_chat_id: number | null }
type Auth = { user: User; access_token: string }

const headers = (token: string) => ({ Authorization: `Bearer ${token}` })
async function api<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const result = await fetch(`${API}${path}`, { ...init, headers: { ...headers(token), ...init.headers } })
  if (!result.ok) throw new Error((await result.json().catch(() => null))?.detail || 'Request failed.')
  return result.json()
}
async function auth(): Promise<Auth> {
  const initData = window.Telegram?.WebApp?.initData
  if (!initData) throw new Error('Open SS Board inside Telegram to authenticate securely.')
  const result = await fetch(`${API}/auth/telegram?init_data=${encodeURIComponent(initData)}`, { method: 'POST' })
  if (!result.ok) throw new Error('Telegram verification failed. Reopen the app from the bot.')
  return result.json()
}
const due = (value: string) => new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
const Status = ({ value }: { value: string }) => <span className={`status status--${value}`}>{value.replaceAll('_', ' ')}</span>

function TaskSheet({ task, user, token, close }: { task: Task; user: User; token: string; close: () => void }) {
  const [notice, setNotice] = useState('')
  const detail = useQuery({ queryKey: ['task', task.id], queryFn: () => api<Detail>(`/tasks/${task.id}`, token) })
  const chat = useQuery({ queryKey: ['chat', task.id], queryFn: () => api<Chat>(`/tasks/${task.id}/chat`, token), retry: false })
  const check = useMutation({ mutationFn: (item: Item) => api(`/tasks/${task.id}/checklist/${item.id}`, token, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_completed: !item.is_completed }) }), onSuccess: () => client.invalidateQueries({ queryKey: ['task', task.id] }) })
  const { register, handleSubmit, reset } = useForm<{ comment: string }>()
  const report = useMutation({ mutationFn: (data: { comment: string }) => api(`/tasks/${task.id}/report`, token, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }), onSuccess: () => { reset(); setNotice('Report submitted. Add photos from the report section.'); client.invalidateQueries({ queryKey: ['task', task.id] }); client.invalidateQueries({ queryKey: ['tasks', user.id] }) } })
  const openChat = () => { if (chat.data?.telegram_chat_id) window.open(`https://t.me/c/${String(chat.data.telegram_chat_id).replace('-100', '')}`, '_blank') }
  return <section className="task-sheet"><div className="composer__heading"><div><span className="eyebrow">Field sheet</span><h2>{task.title}</h2></div><button className="icon-button" onClick={close} aria-label="Close task">×</button></div>{detail.isLoading && <p>Loading task…</p>}{detail.error && <p className="form-error">{detail.error.message}</p>}{detail.data && <><div className="task-meta"><Status value={detail.data.status} /><span>Due {due(detail.data.deadline)}</span></div><p className="task-description">{detail.data.description || 'No task description was provided.'}</p><section className="sheet-section"><div className="section-heading"><h3>Checklist</h3><span>{detail.data.checklist.filter(item => item.is_completed).length}/{detail.data.checklist.length}</span></div>{detail.data.checklist.map(item => <button className={`check-item ${item.is_completed ? 'check-item--done' : ''}`} key={item.id} onClick={() => check.mutate(item)}><span>{item.is_completed ? '✓' : ''}</span>{item.title}</button>)}{detail.data.checklist.length === 0 && <p className="muted">No checklist items.</p>}</section><section className="sheet-section chat-state"><div><h3>Working group</h3><p>{chat.data?.status === 'ready' ? 'Ready for the team.' : task.kind === 'group' ? 'Preparing the group.' : 'Not required for this individual task.'}</p></div>{chat.data?.telegram_chat_id && <button className="secondary" onClick={openChat}>Open chat</button>}</section>{['active', 'returned', 'overdue'].includes(detail.data.status) && <section className="sheet-section report-form"><h3>Submit report</h3><form onSubmit={handleSubmit(data => report.mutate(data))}><label>Comment<textarea {...register('comment')} rows={3} placeholder="What was completed? Anything to note?" /></label><button className="primary" disabled={report.isPending}>{report.isPending ? 'Submitting…' : 'Submit report'}</button></form></section>}{notice && <p className="notice" role="status">{notice}</p>}{report.error && <p className="form-error">{report.error.message}</p>}</>}</section>
}

function App() {
  const [selected, setSelected] = useState<Task | null>(null)
  const session = useQuery({ queryKey: ['auth'], queryFn: auth, retry: false })
  const tasks = useQuery({ queryKey: ['tasks', session.data?.user.id], queryFn: () => api<Task[]>('/tasks', session.data!.access_token), enabled: Boolean(session.data) })
  useEffect(() => { window.Telegram?.WebApp?.ready(); window.Telegram?.WebApp?.expand() }, [])
  if (session.isLoading) return <main className="centered">Opening your board…</main>
  if (session.error) return <main className="gate"><span className="mark">SS</span><h1>SS Board</h1><p>{session.error.message}</p></main>
  const { user, access_token: token } = session.data!
  const open = tasks.data?.filter(task => ['active', 'returned', 'overdue'].includes(task.status)).length ?? 0
  return <main className="shell"><header><div className="brand"><span className="mark">SS</span><div><span className="eyebrow">Operations field board</span><h1>Today’s work</h1></div></div><div className="profile"><strong>{user.full_name || 'Finish profile'}</strong><span>{user.telegram_username ? `@${user.telegram_username}` : 'Telegram user'}</span></div></header><section className="summary"><div><span>Assigned</span><strong>{tasks.data?.length ?? 0}</strong></div><div><span>Open now</span><strong>{open}</strong></div><div><span>Role</span><strong>{user.role.replace('_', ' ')}</strong></div></section><section className="task-list"><div className="section-heading"><span className="eyebrow">Your queue</span><h2>Tasks</h2></div>{tasks.isLoading && <p>Loading tasks…</p>}{tasks.error && <p className="form-error">Tasks could not be loaded.</p>}{tasks.data?.length === 0 && <div className="empty"><span>◌</span><h3>Nothing assigned yet</h3><p>New work will appear here as soon as it is assigned.</p></div>}{tasks.data?.map(task => <button className="task-card" key={task.id} onClick={() => setSelected(task)}><div><Status value={task.status} /><h3>{task.title}</h3><p>{task.description || 'No description provided.'}</p></div><time>{due(task.deadline)}</time></button>)}</section>{selected && <div className="sheet-backdrop"><TaskSheet task={selected} user={user} token={token} close={() => setSelected(null)} /></div>}</main>
}
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
