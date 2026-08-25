import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
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
type TaskDraft = { title: string; deadline: string; kind: 'individual' | 'group'; description: string }

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

function TaskComposer({ user, token, close }: { user: User; token: string; close: () => void }) {
  const [query, setQuery] = useState('')
  const [members, setMembers] = useState<User[]>([])
  const [leaderId, setLeaderId] = useState('')
  const people = useQuery({ queryKey: ['user-search', query], queryFn: () => api<User[]>(`/users/search?q=${encodeURIComponent(query)}`, token), enabled: query.trim().length >= 2 })
  const { register, handleSubmit, watch } = useForm<TaskDraft>({ defaultValues: { kind: 'individual' } })
  const kind = watch('kind')
  const create = useMutation({ mutationFn: (data: TaskDraft) => api<Task>('/tasks', token, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ ...data, deadline: new Date(data.deadline).toISOString(), leader_id: leaderId || null, member_ids: members.map(person => person.id) }) }), onSuccess: () => { client.invalidateQueries({ queryKey: ['tasks', user.id] }); close() } })
  const pick = (person: User) => { if (!members.some(member => member.id === person.id)) setMembers(current => [...current, person]); setQuery('') }
  return <section className="composer"><div className="composer__heading"><div><span className="eyebrow">Dispatch desk</span><h2>Assign work</h2></div><button className="icon-button" onClick={close} aria-label="Close task composer">×</button></div><form onSubmit={handleSubmit(data => create.mutate(data))}><label>Task title<input {...register('title', { required: true })} autoFocus placeholder="Prepare the venue" /></label><label>Deadline<input type="datetime-local" {...register('deadline', { required: true })} /></label><label>Type<select {...register('kind')}><option value="individual">Individual</option><option value="group">Group</option></select></label><label>Description<textarea {...register('description')} rows={2} placeholder="What good work looks like" /></label><label>Find people<input value={query} onChange={event => setQuery(event.target.value)} placeholder="Full name or @username" /></label>{people.data?.map(person => <button className="person-result" type="button" key={person.id} onClick={() => pick(person)}><strong>{person.full_name}</strong><span>{person.telegram_username ? `@${person.telegram_username}` : 'Telegram user'}</span></button>)}<div className="chips">{members.map(person => <button type="button" key={person.id} className="chip" onClick={() => { setMembers(current => current.filter(member => member.id !== person.id)); if (leaderId === person.id) setLeaderId('') }}>{person.full_name} ×</button>)}</div>{kind === 'group' && <label>Group leader<select value={leaderId} onChange={event => setLeaderId(event.target.value)}><option value="">Choose a selected member</option>{members.map(person => <option value={person.id} key={person.id}>{person.full_name}</option>)}</select></label>}{create.error && <p className="form-error">{create.error.message}</p>}<button className="primary" disabled={create.isPending || members.length === 0 || (kind === 'group' && !leaderId)}>{create.isPending ? 'Creating…' : 'Create task'}</button></form></section>
}

function TaskSheet({ task, user, token, close }: { task: Task; user: User; token: string; close: () => void }) {
  const [notice, setNotice] = useState('')
  const photoInput = useRef<HTMLInputElement>(null)
  const detail = useQuery({ queryKey: ['task', task.id], queryFn: () => api<Detail>(`/tasks/${task.id}`, token) })
  const chat = useQuery({ queryKey: ['chat', task.id], queryFn: () => api<Chat>(`/tasks/${task.id}/chat`, token), retry: false })
  const check = useMutation({ mutationFn: (item: Item) => api(`/tasks/${task.id}/checklist/${item.id}`, token, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_completed: !item.is_completed }) }), onSuccess: () => client.invalidateQueries({ queryKey: ['task', task.id] }) })
  const { register, handleSubmit, reset } = useForm<{ comment: string }>()
  const report = useMutation({ mutationFn: (data: { comment: string }) => api(`/tasks/${task.id}/report`, token, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }), onSuccess: () => { reset(); setNotice('Report submitted. Add photos from the report section.'); client.invalidateQueries({ queryKey: ['task', task.id] }); client.invalidateQueries({ queryKey: ['tasks', user.id] }) } })
  const photo = useMutation({ mutationFn: async (file: File) => {
    const target = await api<{ object_key: string; upload_url: string }>(`/tasks/${task.id}/report/upload`, token, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }) })
    const uploaded = await fetch(target.upload_url, { method: 'PUT', headers: { 'Content-Type': file.type }, body: file })
    if (!uploaded.ok) throw new Error('The photo could not be uploaded to storage.')
    return api(`/tasks/${task.id}/report/photos/complete`, token, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ object_key: target.object_key }) })
  }, onSuccess: () => setNotice('Photo saved and previewed.'), })
  const openChat = () => { if (chat.data?.telegram_chat_id) window.open(`https://t.me/c/${String(chat.data.telegram_chat_id).replace('-100', '')}`, '_blank') }
  return <section className="task-sheet"><div className="composer__heading"><div><span className="eyebrow">Field sheet</span><h2>{task.title}</h2></div><button className="icon-button" onClick={close} aria-label="Close task">×</button></div>{detail.isLoading && <p>Loading task…</p>}{detail.error && <p className="form-error">{detail.error.message}</p>}{detail.data && <><div className="task-meta"><Status value={detail.data.status} /><span>Due {due(detail.data.deadline)}</span></div><p className="task-description">{detail.data.description || 'No task description was provided.'}</p><section className="sheet-section"><div className="section-heading"><h3>Checklist</h3><span>{detail.data.checklist.filter(item => item.is_completed).length}/{detail.data.checklist.length}</span></div>{detail.data.checklist.map(item => <button className={`check-item ${item.is_completed ? 'check-item--done' : ''}`} key={item.id} onClick={() => check.mutate(item)}><span>{item.is_completed ? '✓' : ''}</span>{item.title}</button>)}{detail.data.checklist.length === 0 && <p className="muted">No checklist items.</p>}</section><section className="sheet-section chat-state"><div><h3>Working group</h3><p>{chat.data?.status === 'ready' ? 'Ready for the team.' : task.kind === 'group' ? 'Preparing the group.' : 'Not required for this individual task.'}</p></div>{chat.data?.telegram_chat_id && <button className="secondary" onClick={openChat}>Open chat</button>}</section>{['active', 'returned', 'overdue'].includes(detail.data.status) && <section className="sheet-section report-form"><h3>Submit report</h3><form onSubmit={handleSubmit(data => report.mutate(data))}><label>Comment<textarea {...register('comment')} rows={3} placeholder="What was completed? Anything to note?" /></label><button className="primary" disabled={report.isPending}>{report.isPending ? 'Submitting…' : 'Submit report'}</button></form></section>}{['submitted', 'completed'].includes(detail.data.status) && <section className="sheet-section"><h3>Report photos</h3><p className="muted">JPEG, PNG, or WebP - up to 10 MB each, five photos maximum.</p><input ref={photoInput} className="visually-hidden" type="file" accept="image/jpeg,image/png,image/webp" onChange={event => { const file = event.target.files?.[0]; if (file) photo.mutate(file) }} /><button className="secondary" disabled={photo.isPending} onClick={() => photoInput.current?.click()}>{photo.isPending ? 'Uploading…' : 'Add photo'}</button></section>}{notice && <p className="notice" role="status">{notice}</p>}{report.error && <p className="form-error">{report.error.message}</p>}{photo.error && <p className="form-error">{photo.error.message}</p>}</>}</section>
}

function App() {
  const [selected, setSelected] = useState<Task | null>(null)
  const [composing, setComposing] = useState(false)
  const session = useQuery({ queryKey: ['auth'], queryFn: auth, retry: false })
  const tasks = useQuery({ queryKey: ['tasks', session.data?.user.id], queryFn: () => api<Task[]>('/tasks', session.data!.access_token), enabled: Boolean(session.data) })
  useEffect(() => { window.Telegram?.WebApp?.ready(); window.Telegram?.WebApp?.expand() }, [])
  if (session.isLoading) return <main className="centered">Opening your board…</main>
  if (session.error) return <main className="gate"><span className="mark">SS</span><h1>SS Board</h1><p>{session.error.message}</p></main>
  const { user, access_token: token } = session.data!
  const open = tasks.data?.filter(task => ['active', 'returned', 'overdue'].includes(task.status)).length ?? 0
  return <main className="shell"><header><div className="brand"><span className="mark">SS</span><div><span className="eyebrow">Operations field board</span><h1>Today’s work</h1></div></div><div className="profile"><strong>{user.full_name || 'Finish profile'}</strong><span>{user.telegram_username ? `@${user.telegram_username}` : 'Telegram user'}</span></div></header><section className="summary"><div><span>Assigned</span><strong>{tasks.data?.length ?? 0}</strong></div><div><span>Open now</span><strong>{open}</strong></div><div><span>Role</span><strong>{user.role.replace('_', ' ')}</strong></div></section>{user.role !== 'participant' && <button className="new-task" onClick={() => setComposing(true)}><span>+</span> Assign work</button>}<section className="task-list"><div className="section-heading"><span className="eyebrow">Your queue</span><h2>Tasks</h2></div>{tasks.isLoading && <p>Loading tasks…</p>}{tasks.error && <p className="form-error">Tasks could not be loaded.</p>}{tasks.data?.length === 0 && <div className="empty"><span>◌</span><h3>Nothing assigned yet</h3><p>New work will appear here as soon as it is assigned.</p></div>}{tasks.data?.map(task => <button className="task-card" key={task.id} onClick={() => setSelected(task)}><div><Status value={task.status} /><h3>{task.title}</h3><p>{task.description || 'No description provided.'}</p></div><time>{due(task.deadline)}</time></button>)}</section>{(selected || composing) && <div className="sheet-backdrop">{composing ? <TaskComposer user={user} token={token} close={() => setComposing(false)} /> : <TaskSheet task={selected!} user={user} token={token} close={() => setSelected(null)} />}</div>}</main>
}
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
