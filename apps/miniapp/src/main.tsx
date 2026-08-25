import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { useForm } from 'react-hook-form'
import './styles.css'

const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api/v1'
const queryClient = new QueryClient()

type User = { id: string; full_name: string | null; telegram_username: string | null; role: 'participant' | 'sector_head' | 'admin'; status: string }
type Task = { id: string; title: string; description: string | null; status: string; deadline: string; kind: string }
type TaskForm = { title: string; deadline: string; kind: 'individual' | 'group'; leaderId: string; memberIds: string }
type AuthResponse = { user: User; access_token: string }

function telegramInitData(): string {
  return window.Telegram?.WebApp?.initData ?? ''
}

async function authenticate(): Promise<AuthResponse> {
  const initData = telegramInitData()
  if (!initData) throw new Error('Open SS Board inside Telegram to authenticate securely.')
  const response = await fetch(`${API}/auth/telegram?init_data=${encodeURIComponent(initData)}`, { method: 'POST' })
  if (!response.ok) throw new Error('Telegram verification failed. Please reopen the app from the bot.')
  return response.json()
}

async function fetchTasks(token: string): Promise<Task[]> {
  const response = await fetch(`${API}/tasks`, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok) throw new Error('Tasks could not be loaded')
  return response.json()
}

function StatusPill({ status }: { status: string }) { return <span className={`status status--${status}`}>{status.replace('_', ' ')}</span> }

function TaskCard({ task }: { task: Task }) {
  const due = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(task.deadline))
  return <article className="task-card"><div><StatusPill status={task.status} /><h3>{task.title}</h3><p>{task.description || 'No description provided.'}</p></div><time>{due}</time></article>
}

function TaskComposer({ user, token, onClose }: { user: User; token: string; onClose: () => void }) {
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<TaskForm>({ defaultValues: { kind: 'individual' } })
  const mutation = useMutation({
    mutationFn: async (data: TaskForm) => {
      const members = data.memberIds.split(',').map(value => value.trim()).filter(Boolean)
      const response = await fetch(`${API}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ title: data.title, deadline: new Date(data.deadline).toISOString(), kind: data.kind, leader_id: data.leaderId || null, member_ids: members }) })
      if (!response.ok) throw new Error((await response.json()).detail || 'Task could not be created')
      return response.json()
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['tasks', user.id] }); onClose() },
  })
  return <section className="composer"><div className="composer__heading"><div><span className="eyebrow">Dispatch desk</span><h2>New task</h2></div><button className="icon-button" onClick={onClose} aria-label="Close task form">×</button></div><form onSubmit={handleSubmit(data => mutation.mutate(data))}><label>Task title<input {...register('title', { required: 'Give the task a title' })} placeholder="Prepare the venue" autoFocus /></label>{errors.title && <p className="form-error">{errors.title.message}</p>}<label>Deadline<input type="datetime-local" {...register('deadline', { required: 'Set a deadline' })} /></label><label>Type<select {...register('kind')}><option value="individual">Individual</option><option value="group">Group</option></select></label><label>Leader ID <span className="optional">(group tasks)</span><input {...register('leaderId')} placeholder="UUID from user search" /></label><label>Member IDs <span className="optional">(comma separated)</span><input {...register('memberIds')} placeholder="UUID, UUID" /></label>{mutation.error && <p className="form-error">{mutation.error.message}</p>}<button className="primary" disabled={isSubmitting}>{isSubmitting ? 'Creating…' : 'Create task'}</button></form></section>
}

function App() {
  const [showComposer, setShowComposer] = useState(false)
  const auth = useQuery({ queryKey: ['auth'], queryFn: authenticate, retry: false })
  const tasks = useQuery({ queryKey: ['tasks', auth.data?.user.id], queryFn: () => fetchTasks(auth.data!.access_token), enabled: Boolean(auth.data?.user.id) })
  useEffect(() => { window.Telegram?.WebApp?.ready(); window.Telegram?.WebApp?.expand() }, [])
  if (auth.isLoading) return <main className="centered">Opening your board…</main>
  if (auth.error) return <main className="gate"><span className="mark">SS</span><h1>SS Board</h1><p>{auth.error.message}</p></main>
  const { user, access_token: accessToken } = auth.data!
  const canCreate = user.role !== 'participant'
  return <main className="shell"><header><div className="brand"><span className="mark">SS</span><div><span className="eyebrow">Operations field board</span><h1>Today’s work</h1></div></div><div className="profile"><strong>{user.full_name || 'Finish profile'}</strong><span>{user.telegram_username ? `@${user.telegram_username}` : 'Telegram user'}</span></div></header><section className="summary"><div><span>Assigned</span><strong>{tasks.data?.length ?? 0}</strong></div><div><span>Open now</span><strong>{tasks.data?.filter(task => ['active', 'returned', 'overdue'].includes(task.status)).length ?? 0}</strong></div><div><span>Role</span><strong>{user.role.replace('_', ' ')}</strong></div></section>{canCreate && <button className="new-task" onClick={() => setShowComposer(true)}><span>+</span> Assign work</button>}<section className="task-list"><div className="section-heading"><span className="eyebrow">Your queue</span><h2>Tasks</h2></div>{tasks.isLoading && <p>Loading tasks…</p>}{tasks.error && <p className="form-error">Tasks could not be loaded.</p>}{tasks.data?.length === 0 && <div className="empty"><span>◌</span><h3>Nothing assigned yet</h3><p>New work will appear here as soon as it is assigned.</p></div>}{tasks.data?.map(task => <TaskCard key={task.id} task={task} />)}</section>{showComposer && <div className="sheet-backdrop"><TaskComposer user={user} token={accessToken} onClose={() => setShowComposer(false)} /></div>}</main>
}

createRoot(document.getElementById('root')!).render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>)
