import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { useForm } from 'react-hook-form'
import './styles.css'

const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api/v1'
const client = new QueryClient()

type Role = 'participant' | 'sector_head' | 'admin'
type User = {
  id: string
  full_name: string | null
  telegram_username: string | null
  role: Role
  status?: string
  sector_id?: string | null
}
type Task = {
  id: string
  title: string
  description: string | null
  status: string
  deadline: string
  kind: 'individual' | 'group'
  creator_id: string
  leader_id: string | null
  cleanup_at: string | null
}
type Item = {
  id: string
  title: string
  position: number
  is_completed: boolean
  completed_by_id?: string | null
  completed_at?: string | null
}
type TaskMember = { user: User; is_creator: boolean; is_leader: boolean }
type Detail = Task & { checklist: Item[]; members: TaskMember[] }
type ChatMember = {
  user: User
  state: string
  next_reminder_at: string | null
  last_reminder_at: string | null
  reminder_count: number
  joined_at: string | null
  last_checked_at: string | null
  last_error: string | null
}
type Chat = {
  id: string
  status: string
  telegram_chat_id: number | null
  last_error: string | null
  cleanup_warned_at: string | null
  members: ChatMember[]
}
type Photo = {
  id: string
  content_type: string
  size_bytes: number
  width: number | null
  height: number | null
  original_url: string | null
  preview_url: string | null
}
type Report = {
  id: string
  task_id: string
  submitted_by_id: string
  status: 'draft' | 'submitted' | 'returned' | 'approved'
  comment: string | null
  approval_comment: string | null
  submitted_at: string
  returned_at: string | null
  approved_at: string | null
  photos: Photo[]
}
type Auth = { user: User; access_token: string }
type Event = {
  id: string
  title: string
  description?: string | null
  starts_at: string
  ends_at?: string | null
  budget?: number | null
  sector_id?: string | null
  retention_delete_at: string | null
  retention_extended_until?: string | null
}
type Archive = {
  event: Event
  participants: User[]
  tasks: { id: string; title: string; status: string; report: { photo_count: number } | null }[]
}
type AdminUser = User & { status: string }
type TaskDraft = {
  title: string
  deadline: string
  kind: 'individual' | 'group'
  description: string
  event_id: string
  checklist: string
}
type EventDraft = {
  title: string
  description: string
  starts_at: string
  ends_at: string
  budget: string
}

const headers = (token: string) => ({ Authorization: `Bearer ${token}` })

async function parseError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null)
  if (typeof body?.detail === 'string') return body.detail
  if (Array.isArray(body?.detail)) {
    return body.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join('; ')
  }
  return `Request failed (${response.status}).`
}

async function api<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const result = await fetch(`${API}${path}`, {
    ...init,
    headers: { ...headers(token), ...init.headers },
  })
  if (!result.ok) throw new Error(await parseError(result))
  if (result.status === 204) return undefined as T
  const text = await result.text()
  return (text ? JSON.parse(text) : undefined) as T
}

async function apiOptional<T>(path: string, token: string): Promise<T | null> {
  const result = await fetch(`${API}${path}`, { headers: headers(token) })
  if (result.status === 404) return null
  if (!result.ok) throw new Error(await parseError(result))
  return result.json()
}

async function auth(): Promise<Auth> {
  const initData = window.Telegram?.WebApp?.initData
  if (!initData) throw new Error('Open SS Board inside Telegram to authenticate securely.')
  const result = await fetch(`${API}/auth/telegram?init_data=${encodeURIComponent(initData)}`, {
    method: 'POST',
  })
  if (!result.ok) throw new Error('Telegram verification failed. Reopen the app from the bot.')
  return result.json()
}

const due = (value: string) =>
  new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
const personName = (person: User) =>
  person.full_name || (person.telegram_username ? `@${person.telegram_username}` : 'Telegram user')
const Status = ({ value }: { value: string }) => (
  <span className={`status status--${value}`}>{value.replaceAll('_', ' ')}</span>
)
const isManager = (user: User) => user.role === 'admin' || user.role === 'sector_head'
const editableStatuses = ['active', 'returned', 'overdue']

async function download(path: string, token: string, filename: string) {
  const response = await fetch(`${API}${path}`, { headers: headers(token) })
  if (!response.ok) throw new Error('Export is temporarily unavailable.')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function PersonSearch({
  token,
  selected,
  onPick,
}: {
  token: string
  selected: User[]
  onPick: (person: User) => void
}) {
  const [query, setQuery] = useState('')
  const people = useQuery({
    queryKey: ['user-search', query],
    queryFn: () => api<User[]>(`/users/search?q=${encodeURIComponent(query)}`, token),
    enabled: query.trim().length >= 2,
  })
  return (
    <>
      <label>
        Find people
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Full name or @username"
        />
      </label>
      {people.error && <p className="form-error">{people.error.message}</p>}
      {people.data
        ?.filter((person) => !selected.some((chosen) => chosen.id === person.id))
        .map((person) => (
          <button
            className="person-result"
            type="button"
            key={person.id}
            onClick={() => {
              onPick(person)
              setQuery('')
            }}
          >
            <strong>{personName(person)}</strong>
            <span>{person.telegram_username ? `@${person.telegram_username}` : 'No username'}</span>
          </button>
        ))}
    </>
  )
}

function ArchiveSheet({
  event,
  user,
  token,
  close,
}: {
  event: Event
  user: User
  token: string
  close: () => void
}) {
  const [retentionUntil, setRetentionUntil] = useState('')
  const archive = useQuery({
    queryKey: ['archive', event.id],
    queryFn: () => api<Archive>(`/events/${event.id}/archive`, token),
  })
  const exportFile = useMutation({
    mutationFn: ({ path, name }: { path: string; name: string }) => download(path, token, name),
  })
  const extend = useMutation({
    mutationFn: () =>
      api<Event>(`/events/${event.id}/retention/extend`, token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ until: new Date(retentionUntil).toISOString() }),
      }),
    onSuccess: () => {
      setRetentionUntil('')
      client.invalidateQueries({ queryKey: ['events'] })
      client.invalidateQueries({ queryKey: ['archive', event.id] })
    },
  })
  return (
    <section className="task-sheet">
      <div className="composer__heading">
        <div>
          <span className="eyebrow">Event record</span>
          <h2>{event.title}</h2>
        </div>
        <button className="icon-button" onClick={close} aria-label="Close archive">×</button>
      </div>
      {archive.isLoading && <p>Loading archive…</p>}
      {archive.error && <p className="form-error">{archive.error.message}</p>}
      {archive.data && (
        <>
          <div className="task-meta">
            <span>{archive.data.participants.length} participants</span>
            <span>{archive.data.tasks.length} tasks</span>
          </div>
          <section className="sheet-section">
            <h3>Participants</h3>
            <div className="member-list">
              {archive.data.participants.map((person) => (
                <div className="member-row" key={person.id}>
                  <span>{personName(person)}</span>
                  <small>{person.telegram_username ? `@${person.telegram_username}` : 'No username'}</small>
                </div>
              ))}
            </div>
          </section>
          <section className="sheet-section">
            <h3>Archive contents</h3>
            {archive.data.tasks.map((task) => (
              <div className="archive-row" key={task.id}>
                <span>{task.title}</span>
                <Status value={task.status} />
                <small>{task.report?.photo_count ?? 0} photos</small>
              </div>
            ))}
          </section>
          <section className="sheet-section export-actions">
            <h3>Downloads</h3>
            <button
              className="secondary"
              disabled={exportFile.isPending}
              onClick={() =>
                exportFile.mutate({
                  path: `/events/${event.id}/exports/pdf`,
                  name: `${event.title}-archive.pdf`,
                })
              }
            >
              Download PDF
            </button>
            <button
              className="secondary"
              disabled={exportFile.isPending}
              onClick={() =>
                exportFile.mutate({
                  path: `/events/${event.id}/exports/photos`,
                  name: `${event.title}-photos.zip`,
                })
              }
            >
              Download photos ZIP
            </button>
          </section>
          {user.role === 'admin' && archive.data.event.retention_delete_at && (
            <section className="sheet-section">
              <h3>Archive retention</h3>
              <p className="muted">
                Current deletion date: {due(archive.data.event.retention_delete_at)}
              </p>
              <label>
                Extend until
                <input
                  type="datetime-local"
                  value={retentionUntil}
                  onChange={(event) => setRetentionUntil(event.target.value)}
                />
              </label>
              <button
                className="secondary"
                disabled={!retentionUntil || extend.isPending}
                onClick={() => extend.mutate()}
              >
                Extend retention
              </button>
              {extend.error && <p className="form-error">{extend.error.message}</p>}
            </section>
          )}
        </>
      )}
      {exportFile.error && <p className="form-error">{exportFile.error.message}</p>}
    </section>
  )
}

function AdminSheet({ token, close }: { token: string; close: () => void }) {
  const users = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api<AdminUser[]>('/admin/users', token),
  })
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: object }) =>
      api(`/admin/users/${id}`, token, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['admin-users'] }),
  })
  return (
    <section className="task-sheet">
      <div className="composer__heading">
        <div>
          <span className="eyebrow">Administration</span>
          <h2>People</h2>
        </div>
        <button className="icon-button" onClick={close} aria-label="Close administration">×</button>
      </div>
      {users.isLoading && <p>Loading people…</p>}
      {users.error && <p className="form-error">{users.error.message}</p>}
      {users.data?.map((person) => (
        <div className="admin-user" key={person.id}>
          <div>
            <strong>{person.full_name || 'Unfinished profile'}</strong>
            <span>
              {person.telegram_username ? `@${person.telegram_username}` : 'No username'} ·{' '}
              {person.status}
            </span>
          </div>
          <select
            value={person.role}
            onChange={(event) =>
              update.mutate({ id: person.id, patch: { role: event.target.value } })
            }
          >
            <option value="participant">Participant</option>
            <option value="sector_head">Sector head</option>
            <option value="admin">Administrator</option>
          </select>
          <button
            className="secondary"
            disabled={update.isPending}
            onClick={() =>
              update.mutate({
                id: person.id,
                patch: { status: person.status === 'inactive' ? 'active' : 'inactive' },
              })
            }
          >
            {person.status === 'inactive' ? 'Activate' : 'Deactivate'}
          </button>
        </div>
      ))}
      {update.error && <p className="form-error">{update.error.message}</p>}
    </section>
  )
}

function EventComposer({ token, close }: { token: string; close: () => void }) {
  const [participants, setParticipants] = useState<User[]>([])
  const { register, handleSubmit } = useForm<EventDraft>()
  const create = useMutation({
    mutationFn: (data: EventDraft) =>
      api<Event>('/events', token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: data.title,
          description: data.description || null,
          starts_at: new Date(data.starts_at).toISOString(),
          ends_at: data.ends_at ? new Date(data.ends_at).toISOString() : null,
          budget: data.budget ? Number(data.budget) : null,
          participant_ids: participants.map((person) => person.id),
        }),
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['events'] })
      close()
    },
  })
  return (
    <section className="composer">
      <div className="composer__heading">
        <div>
          <span className="eyebrow">Planning</span>
          <h2>Create event</h2>
        </div>
        <button className="icon-button" onClick={close} aria-label="Close event composer">×</button>
      </div>
      <form onSubmit={handleSubmit((data) => create.mutate(data))}>
        <label>
          Event title
          <input {...register('title', { required: true })} autoFocus placeholder="Community day" />
        </label>
        <label>
          Starts
          <input type="datetime-local" {...register('starts_at', { required: true })} />
        </label>
        <label>
          Ends <span className="optional">optional</span>
          <input type="datetime-local" {...register('ends_at')} />
        </label>
        <label>
          Budget <span className="optional">optional</span>
          <input type="number" min="0" step="0.01" {...register('budget')} />
        </label>
        <label>
          Description <span className="optional">optional</span>
          <textarea {...register('description')} rows={3} />
        </label>
        <PersonSearch
          token={token}
          selected={participants}
          onPick={(person) => setParticipants((current) => [...current, person])}
        />
        <div className="chips">
          {participants.map((person) => (
            <button
              type="button"
              key={person.id}
              className="chip"
              onClick={() =>
                setParticipants((current) => current.filter((item) => item.id !== person.id))
              }
            >
              {personName(person)} ×
            </button>
          ))}
        </div>
        {create.error && <p className="form-error">{create.error.message}</p>}
        <button className="primary" disabled={create.isPending}>
          {create.isPending ? 'Creating…' : 'Create event'}
        </button>
      </form>
    </section>
  )
}

function TaskComposer({
  user,
  token,
  events,
  close,
}: {
  user: User
  token: string
  events: Event[]
  close: () => void
}) {
  const [members, setMembers] = useState<User[]>([])
  const [leaderId, setLeaderId] = useState('')
  const { register, handleSubmit, watch } = useForm<TaskDraft>({
    defaultValues: { kind: 'individual', event_id: '', checklist: '' },
  })
  const kind = watch('kind')
  useEffect(() => {
    if (kind === 'individual') setLeaderId('')
  }, [kind])
  const validMembership =
    kind === 'individual' ? members.length === 1 : members.length > 0 && Boolean(leaderId)
  const create = useMutation({
    mutationFn: (data: TaskDraft) =>
      api<Task>('/tasks', token, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': crypto.randomUUID(),
        },
        body: JSON.stringify({
          title: data.title,
          description: data.description || null,
          deadline: new Date(data.deadline).toISOString(),
          kind: data.kind,
          event_id: data.event_id || null,
          leader_id: data.kind === 'group' ? leaderId : null,
          member_ids: members.map((person) => person.id),
          checklist: data.checklist
            .split('\n')
            .map((item) => item.trim())
            .filter(Boolean),
        }),
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['tasks', user.id] })
      close()
    },
  })
  return (
    <section className="composer">
      <div className="composer__heading">
        <div>
          <span className="eyebrow">Dispatch desk</span>
          <h2>Assign work</h2>
        </div>
        <button className="icon-button" onClick={close} aria-label="Close task composer">×</button>
      </div>
      <form onSubmit={handleSubmit((data) => create.mutate(data))}>
        <label>
          Task title
          <input
            {...register('title', { required: true })}
            autoFocus
            placeholder="Prepare the venue"
          />
        </label>
        <label>
          Deadline
          <input type="datetime-local" {...register('deadline', { required: true })} />
        </label>
        <label>
          Type
          <select {...register('kind')}>
            <option value="individual">Individual</option>
            <option value="group">Group</option>
          </select>
        </label>
        <label>
          Event <span className="optional">optional</span>
          <select {...register('event_id')}>
            <option value="">No event</option>
            {events.map((event) => (
              <option value={event.id} key={event.id}>
                {event.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Description <span className="optional">optional</span>
          <textarea
            {...register('description')}
            rows={3}
            placeholder="What good work looks like"
          />
        </label>
        <label>
          Checklist <span className="optional">one item per line</span>
          <textarea
            {...register('checklist')}
            rows={4}
            placeholder={'Prepare chairs\nCheck sound\nConfirm access'}
          />
        </label>
        <PersonSearch
          token={token}
          selected={members}
          onPick={(person) => setMembers((current) => [...current, person])}
        />
        <div className="chips">
          {members.map((person) => (
            <button
              type="button"
              key={person.id}
              className="chip"
              onClick={() => {
                setMembers((current) => current.filter((member) => member.id !== person.id))
                if (leaderId === person.id) setLeaderId('')
              }}
            >
              {personName(person)} ×
            </button>
          ))}
        </div>
        {kind === 'group' && (
          <label>
            Group leader
            <select value={leaderId} onChange={(event) => setLeaderId(event.target.value)}>
              <option value="">Choose a selected member</option>
              {members.map((person) => (
                <option value={person.id} key={person.id}>
                  {personName(person)}
                </option>
              ))}
            </select>
          </label>
        )}
        {kind === 'individual' && members.length > 1 && (
          <p className="form-error">An individual task must have exactly one assignee.</p>
        )}
        {create.error && <p className="form-error">{create.error.message}</p>}
        <button className="primary" disabled={create.isPending || !validMembership}>
          {create.isPending ? 'Creating…' : 'Create task'}
        </button>
      </form>
    </section>
  )
}

function TaskSheet({ task, user, token, close }: { task: Task; user: User; token: string; close: () => void }) {
  const [notice, setNotice] = useState('')
  const [reworkReason, setReworkReason] = useState('')
  const [memberQuery, setMemberQuery] = useState('')
  const [newChecklist, setNewChecklist] = useState('')
  const [editTitle, setEditTitle] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const [editDeadline, setEditDeadline] = useState('')
  const photoInput = useRef<HTMLInputElement>(null)
  const detail = useQuery({
    queryKey: ['task', task.id],
    queryFn: () => api<Detail>(`/tasks/${task.id}`, token),
  })
  const chat = useQuery({
    queryKey: ['chat', task.id],
    queryFn: () => api<Chat>(`/tasks/${task.id}/chat`, token),
    enabled: task.kind === 'group',
    retry: false,
  })
  const reportState = useQuery({
    queryKey: ['report', task.id],
    queryFn: () => apiOptional<Report>(`/tasks/${task.id}/report`, token),
    retry: false,
  })
  const manager = isManager(user)
  const openTask = detail.data ? editableStatuses.includes(detail.data.status) : false
  const { register, handleSubmit, reset, setValue } = useForm<{ comment: string }>()

  useEffect(() => {
    if (reportState.data?.comment !== undefined) setValue('comment', reportState.data?.comment ?? '')
  }, [reportState.data?.comment, setValue])
  useEffect(() => {
    if (!detail.data) return
    setEditTitle(detail.data.title)
    setEditDescription(detail.data.description ?? '')
    const date = new Date(detail.data.deadline)
    const offset = date.getTimezoneOffset() * 60_000
    setEditDeadline(new Date(date.getTime() - offset).toISOString().slice(0, 16))
  }, [detail.data?.id, detail.data?.deadline, detail.data?.title, detail.data?.description])

  const refresh = () => {
    client.invalidateQueries({ queryKey: ['task', task.id] })
    client.invalidateQueries({ queryKey: ['tasks', user.id] })
    client.invalidateQueries({ queryKey: ['report', task.id] })
    client.invalidateQueries({ queryKey: ['chat', task.id] })
  }

  const check = useMutation({
    mutationFn: (item: Item) =>
      api(`/tasks/${task.id}/checklist/${item.id}`, token, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_completed: !item.is_completed }),
      }),
    onSuccess: refresh,
  })
  const submit = useMutation({
    mutationFn: (data: { comment: string }) =>
      api<Report>(`/tasks/${task.id}/report`, token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      reset()
      setNotice(task.kind === 'group' ? 'Report submitted to the group leader.' : 'Task completed.')
      refresh()
    },
  })
  const photo = useMutation({
    mutationFn: async (file: File) => {
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
        throw new Error('Use JPEG, PNG, or WebP.')
      }
      if (file.size > 10 * 1024 * 1024) throw new Error('Photo must be 10 MB or smaller.')
      const target = await api<{ object_key: string; upload_url: string }>(
        `/tasks/${task.id}/report/upload`,
        token,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            filename: file.name,
            content_type: file.type,
            size_bytes: file.size,
          }),
        },
      )
      const uploaded = await fetch(target.upload_url, {
        method: 'PUT',
        headers: { 'Content-Type': file.type },
        body: file,
      })
      if (!uploaded.ok) throw new Error('The photo could not be uploaded to storage.')
      return api<Photo>(`/tasks/${task.id}/report/photos/complete`, token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ object_key: target.object_key }),
      })
    },
    onSuccess: () => {
      setNotice('Photo saved. You can add more before submitting the report.')
      client.invalidateQueries({ queryKey: ['report', task.id] })
    },
  })
  const deletePhoto = useMutation({
    mutationFn: (photoId: string) =>
      api(`/tasks/${task.id}/report/photos/${photoId}`, token, { method: 'DELETE' }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['report', task.id] }),
  })
  const decision = useMutation({
    mutationFn: ({ approved, reason }: { approved: boolean; reason?: string }) =>
      api<Report>(`/tasks/${task.id}/report/decision`, token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved, reason: reason || null }),
      }),
    onSuccess: (_, variables) => {
      setNotice(variables.approved ? 'Report approved. Task completed.' : 'Report returned for rework.')
      setReworkReason('')
      refresh()
    },
  })
  const retryChat = useMutation({
    mutationFn: (mode: 'retry' | 'recover') =>
      api(`/tasks/${task.id}/chat/${mode}`, token, { method: 'POST' }),
    onSuccess: refresh,
  })
  const retryMember = useMutation({
    mutationFn: (userId: string) =>
      api(`/tasks/${task.id}/chat/members/${userId}/retry`, token, { method: 'POST' }),
    onSuccess: refresh,
  })
  const people = useQuery({
    queryKey: ['task-member-search', task.id, memberQuery],
    queryFn: () => api<User[]>(`/users/search?q=${encodeURIComponent(memberQuery)}`, token),
    enabled: manager && task.kind === 'group' && openTask && memberQuery.trim().length >= 2,
  })
  const addMember = useMutation({
    mutationFn: (userId: string) =>
      api(`/tasks/${task.id}/members`, token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId }),
      }),
    onSuccess: () => {
      setMemberQuery('')
      refresh()
    },
  })
  const removeMember = useMutation({
    mutationFn: (userId: string) =>
      api(`/tasks/${task.id}/members/${userId}`, token, { method: 'DELETE' }),
    onSuccess: refresh,
  })
  const changeLeader = useMutation({
    mutationFn: (leaderId: string) =>
      api(`/tasks/${task.id}`, token, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ leader_id: leaderId }),
      }),
    onSuccess: refresh,
  })
  const updateTask = useMutation({
    mutationFn: () =>
      api(`/tasks/${task.id}`, token, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: editTitle,
          description: editDescription || null,
          deadline: new Date(editDeadline).toISOString(),
        }),
      }),
    onSuccess: () => {
      setNotice('Task details updated.')
      refresh()
    },
  })
  const addChecklist = useMutation({
    mutationFn: () =>
      api(`/tasks/${task.id}/checklist`, token, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newChecklist }),
      }),
    onSuccess: () => {
      setNewChecklist('')
      refresh()
    },
  })
  const removeChecklist = useMutation({
    mutationFn: (itemId: string) =>
      api(`/tasks/${task.id}/checklist/${itemId}`, token, { method: 'DELETE' }),
    onSuccess: refresh,
  })
  const cancelTask = useMutation({
    mutationFn: () => api(`/tasks/${task.id}/cancel`, token, { method: 'POST' }),
    onSuccess: () => {
      refresh()
      close()
    },
  })

  const openChat = () => {
    if (!chat.data?.telegram_chat_id) return
    const id = String(chat.data.telegram_chat_id).replace('-100', '')
    window.open(`https://t.me/c/${id}`, '_blank', 'noopener,noreferrer')
  }
  const errors = [
    check.error,
    submit.error,
    photo.error,
    deletePhoto.error,
    decision.error,
    retryChat.error,
    retryMember.error,
    addMember.error,
    removeMember.error,
    changeLeader.error,
    updateTask.error,
    addChecklist.error,
    removeChecklist.error,
    cancelTask.error,
  ].filter(Boolean) as Error[]

  return (
    <section className="task-sheet">
      <div className="composer__heading">
        <div>
          <span className="eyebrow">Field sheet</span>
          <h2>{detail.data?.title ?? task.title}</h2>
        </div>
        <button className="icon-button" onClick={close} aria-label="Close task">×</button>
      </div>
      {detail.isLoading && <p>Loading task…</p>}
      {detail.error && <p className="form-error">{detail.error.message}</p>}
      {detail.data && (
        <>
          <div className="task-meta">
            <Status value={detail.data.status} />
            <span>Due {due(detail.data.deadline)}</span>
          </div>
          <p className="task-description">
            {detail.data.description || 'No task description was provided.'}
          </p>

          <section className="sheet-section">
            <div className="section-heading">
              <h3>Checklist</h3>
              <span>
                {detail.data.checklist.filter((item) => item.is_completed).length}/
                {detail.data.checklist.length}
              </span>
            </div>
            {detail.data.checklist.map((item) => (
              <div className="check-row" key={item.id}>
                <button
                  className={`check-item ${item.is_completed ? 'check-item--done' : ''}`}
                  disabled={!openTask || check.isPending}
                  onClick={() => check.mutate(item)}
                >
                  <span>{item.is_completed ? '✓' : ''}</span>
                  {item.title}
                </button>
                {manager && openTask && (
                  <button
                    className="tiny-danger"
                    onClick={() => removeChecklist.mutate(item.id)}
                    aria-label="Remove item"
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
            {detail.data.checklist.length === 0 && <p className="muted">No checklist items.</p>}
            {manager && openTask && (
              <div className="inline-form">
                <input
                  value={newChecklist}
                  onChange={(event) => setNewChecklist(event.target.value)}
                  placeholder="New checklist item"
                />
                <button
                  className="secondary"
                  disabled={!newChecklist.trim() || addChecklist.isPending}
                  onClick={() => addChecklist.mutate()}
                >
                  Add
                </button>
              </div>
            )}
          </section>

          <section className="sheet-section">
            <div className="section-heading">
              <h3>Team</h3>
              <span>{detail.data.members.length}</span>
            </div>
            <div className="member-list">
              {detail.data.members.map((member) => (
                <div className="member-row" key={member.user.id}>
                  <div>
                    <strong>{personName(member.user)}</strong>
                    <small>
                      {member.is_creator ? 'Creator' : member.is_leader ? 'Leader' : 'Member'}
                      {member.user.telegram_username ? ` · @${member.user.telegram_username}` : ''}
                    </small>
                  </div>
                  {manager &&
                    openTask &&
                    task.kind === 'group' &&
                    !member.is_creator &&
                    !member.is_leader && (
                      <button
                        className="tiny-danger"
                        onClick={() => removeMember.mutate(member.user.id)}
                      >
                        Remove
                      </button>
                    )}
                </div>
              ))}
            </div>
            {manager && openTask && task.kind === 'group' && (
              <>
                <label>
                  Leader
                  <select
                    value={detail.data.leader_id ?? ''}
                    onChange={(event) =>
                      event.target.value && changeLeader.mutate(event.target.value)
                    }
                  >
                    <option value="">Choose leader</option>
                    {detail.data.members
                      .filter((member) => !member.is_creator)
                      .map((member) => (
                        <option value={member.user.id} key={member.user.id}>
                          {personName(member.user)}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  Add member
                  <input
                    value={memberQuery}
                    onChange={(event) => setMemberQuery(event.target.value)}
                    placeholder="Name or @username"
                  />
                </label>
                {people.data
                  ?.filter(
                    (person) =>
                      !detail.data.members.some((member) => member.user.id === person.id),
                  )
                  .map((person) => (
                    <button
                      className="person-result"
                      key={person.id}
                      onClick={() => addMember.mutate(person.id)}
                    >
                      <strong>{personName(person)}</strong>
                      <span>Add</span>
                    </button>
                  ))}
              </>
            )}
          </section>

          {task.kind === 'group' && (
            <section className="sheet-section">
              <div className="section-heading">
                <h3>Working group</h3>
                {chat.data && <Status value={chat.data.status} />}
              </div>
              {chat.isLoading && <p className="muted">Checking Telegram group…</p>}
              {chat.error && <p className="form-error">{chat.error.message}</p>}
              {chat.data?.last_error && <p className="form-error">{chat.data.last_error}</p>}
              {chat.data?.members.map((member) => (
                <div className="chat-member" key={member.user.id}>
                  <div>
                    <strong>{personName(member.user)}</strong>
                    <small>
                      {member.state.replaceAll('_', ' ')}
                      {member.last_error ? ` · ${member.last_error}` : ''}
                    </small>
                  </div>
                  {manager &&
                    member.state !== 'joined' &&
                    member.state !== 'removed' &&
                    chat.data?.status === 'ready' && (
                      <button
                        className="secondary"
                        disabled={retryMember.isPending}
                        onClick={() => retryMember.mutate(member.user.id)}
                      >
                        Retry invite
                      </button>
                    )}
                </div>
              ))}
              <div className="action-row">
                {chat.data?.telegram_chat_id && (
                  <button className="secondary" onClick={openChat}>Open chat</button>
                )}
                {manager && chat.data && ['failed', 'degraded'].includes(chat.data.status) && (
                  <button
                    className="secondary"
                    disabled={retryChat.isPending}
                    onClick={() => retryChat.mutate(chat.data.telegram_chat_id ? 'recover' : 'retry')}
                  >
                    Recover group
                  </button>
                )}
              </div>
            </section>
          )}

          {openTask && (
            <section className="sheet-section">
              <div className="section-heading">
                <h3>Report photos</h3>
                <span>{reportState.data?.photos.length ?? 0}/5</span>
              </div>
              {reportState.data?.status === 'returned' && reportState.data.approval_comment && (
                <p className="rework-note">Returned: {reportState.data.approval_comment}</p>
              )}
              <p className="muted">
                Add photos before final submission. JPEG, PNG or WebP; max 10 MB each.
              </p>
              <div className="photo-grid">
                {reportState.data?.photos.map((item) => (
                  <div className="photo-card" key={item.id}>
                    {item.preview_url ? (
                      <a
                        href={item.original_url ?? item.preview_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <img src={item.preview_url} alt="Report attachment" />
                      </a>
                    ) : (
                      <div className="photo-placeholder">Image</div>
                    )}
                    <button
                      className="tiny-danger"
                      disabled={deletePhoto.isPending}
                      onClick={() => deletePhoto.mutate(item.id)}
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
              <input
                ref={photoInput}
                className="visually-hidden"
                type="file"
                accept="image/jpeg,image/png,image/webp"
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  if (file) photo.mutate(file)
                  event.currentTarget.value = ''
                }}
              />
              <button
                className="secondary"
                disabled={photo.isPending || (reportState.data?.photos.length ?? 0) >= 5}
                onClick={() => photoInput.current?.click()}
              >
                {photo.isPending ? 'Uploading…' : 'Add photo'}
              </button>
            </section>
          )}

          {openTask && (
            <section className="sheet-section report-form">
              <h3>{detail.data.status === 'returned' ? 'Resubmit report' : 'Submit report'}</h3>
              <form onSubmit={handleSubmit((data) => submit.mutate(data))}>
                <label>
                  Comment
                  <textarea
                    {...register('comment')}
                    rows={3}
                    placeholder="What was completed? Anything to note?"
                  />
                </label>
                <button className="primary" disabled={submit.isPending}>
                  {submit.isPending
                    ? 'Submitting…'
                    : detail.data.status === 'returned'
                      ? 'Resubmit'
                      : 'Submit report'}
                </button>
              </form>
            </section>
          )}

          {detail.data.status === 'submitted' && detail.data.leader_id === user.id && (
            <section className="sheet-section leader-decision">
              <h3>Leader review</h3>
              <p className="muted">Review the comment and attachments before closing the task.</p>
              {reportState.data?.comment && (
                <p className="report-comment">{reportState.data.comment}</p>
              )}
              <div className="photo-grid">
                {reportState.data?.photos.map((item) => (
                  <a
                    className="photo-card"
                    key={item.id}
                    href={item.original_url ?? item.preview_url ?? '#'}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {item.preview_url ? (
                      <img src={item.preview_url} alt="Report attachment" />
                    ) : (
                      <div className="photo-placeholder">Image</div>
                    )}
                  </a>
                ))}
              </div>
              <button
                className="primary"
                disabled={decision.isPending}
                onClick={() => decision.mutate({ approved: true })}
              >
                Approve and complete
              </button>
              <label>
                Return reason
                <textarea
                  value={reworkReason}
                  onChange={(event) => setReworkReason(event.target.value)}
                  rows={3}
                />
              </label>
              <button
                className="secondary danger"
                disabled={decision.isPending || !reworkReason.trim()}
                onClick={() => decision.mutate({ approved: false, reason: reworkReason })}
              >
                Return for rework
              </button>
            </section>
          )}

          {!openTask && reportState.data && (
            <section className="sheet-section">
              <h3>Report</h3>
              <div className="task-meta">
                <Status value={reportState.data.status} />
                <span>{reportState.data.photos.length} photos</span>
              </div>
              <p className="report-comment">{reportState.data.comment || 'No comment.'}</p>
              {reportState.data.approval_comment && (
                <p className="rework-note">Review: {reportState.data.approval_comment}</p>
              )}
            </section>
          )}

          {manager && openTask && (
            <section className="sheet-section manager-tools">
              <h3>Manage task</h3>
              <label>
                Title
                <input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} />
              </label>
              <label>
                Description
                <textarea
                  rows={3}
                  value={editDescription}
                  onChange={(event) => setEditDescription(event.target.value)}
                />
              </label>
              <label>
                Deadline
                <input
                  type="datetime-local"
                  value={editDeadline}
                  onChange={(event) => setEditDeadline(event.target.value)}
                />
              </label>
              <div className="action-row">
                <button
                  className="secondary"
                  disabled={!editTitle.trim() || !editDeadline || updateTask.isPending}
                  onClick={() => updateTask.mutate()}
                >
                  Save changes
                </button>
                <button
                  className="secondary danger"
                  disabled={cancelTask.isPending}
                  onClick={() => window.confirm('Cancel this task?') && cancelTask.mutate()}
                >
                  Cancel task
                </button>
              </div>
            </section>
          )}

          {notice && <p className="notice" role="status">{notice}</p>}
          {errors.map((error, index) => (
            <p className="form-error" key={`${error.message}-${index}`}>{error.message}</p>
          ))}
        </>
      )}
    </section>
  )
}

function App() {
  const [selected, setSelected] = useState<Task | null>(null)
  const [taskComposerOpen, setTaskComposerOpen] = useState(false)
  const [eventComposerOpen, setEventComposerOpen] = useState(false)
  const [selectedEvent, setSelectedEvent] = useState<Event | null>(null)
  const [adminOpen, setAdminOpen] = useState(false)
  const session = useQuery({ queryKey: ['auth'], queryFn: auth, retry: false })
  const token = session.data?.access_token ?? ''
  const user = session.data?.user
  const tasks = useQuery({
    queryKey: ['tasks', user?.id],
    queryFn: () => api<Task[]>('/tasks', token),
    enabled: Boolean(user && token),
  })
  const events = useQuery({
    queryKey: ['events'],
    queryFn: () => api<Event[]>('/events', token),
    enabled: Boolean(user && token && user.role !== 'participant'),
  })
  useEffect(() => {
    window.Telegram?.WebApp?.ready()
    window.Telegram?.WebApp?.expand()
  }, [])

  if (session.isLoading) return <main className="centered">Opening your board…</main>
  if (session.error) {
    return (
      <main className="gate">
        <span className="mark">SS</span>
        <h1>SS Board</h1>
        <p>{session.error.message}</p>
      </main>
    )
  }
  if (!user) return <main className="centered">Session unavailable.</main>

  const openTasks = tasks.data?.filter((task) => editableStatuses.includes(task.status)) ?? []
  const submitted = tasks.data?.filter((task) => task.status === 'submitted').length ?? 0
  const overdue = tasks.data?.filter((task) => task.status === 'overdue').length ?? 0
  const manager = isManager(user)

  return (
    <main className="shell">
      <header>
        <div className="brand">
          <span className="mark">SS</span>
          <div>
            <span className="eyebrow">Operations board</span>
            <h1>My work</h1>
          </div>
        </div>
        <div className="profile">
          <strong>{user.full_name || 'Telegram user'}</strong>
          <span>
            {user.telegram_username ? `@${user.telegram_username}` : user.role.replaceAll('_', ' ')}
          </span>
        </div>
      </header>

      <section className="summary">
        <div><span>Open</span><strong>{openTasks.length}</strong></div>
        <div><span>Awaiting review</span><strong>{submitted}</strong></div>
        <div><span>Overdue</span><strong>{overdue}</strong></div>
      </section>

      {manager && (
        <div className="manager-actions">
          <button className="new-task" onClick={() => setTaskComposerOpen(true)}>
            <span>+</span> New task
          </button>
          <button className="secondary manager-action" onClick={() => setEventComposerOpen(true)}>
            New event
          </button>
          {user.role === 'admin' && (
            <button className="secondary manager-action" onClick={() => setAdminOpen(true)}>
              People
            </button>
          )}
        </div>
      )}

      <section className="task-list">
        <div className="section-heading"><h2>Tasks</h2><span>{tasks.data?.length ?? 0}</span></div>
        {tasks.isLoading && <p className="muted">Loading tasks…</p>}
        {tasks.error && <p className="form-error">{tasks.error.message}</p>}
        {tasks.data?.map((task) => (
          <button className="task-card" key={task.id} onClick={() => setSelected(task)}>
            <div>
              <Status value={task.status} />
              <h3>{task.title}</h3>
              <p>{task.description || (task.kind === 'group' ? 'Group task' : 'Individual task')}</p>
            </div>
            <time>{due(task.deadline)}</time>
          </button>
        ))}
        {!tasks.isLoading && tasks.data?.length === 0 && (
          <div className="empty"><span>✓</span><h3>No tasks</h3><p>Your work queue is clear.</p></div>
        )}
      </section>

      {manager && (
        <section className="event-list">
          <div className="section-heading"><h2>Events</h2><span>{events.data?.length ?? 0}</span></div>
          {events.isLoading && <p className="muted">Loading events…</p>}
          {events.error && <p className="form-error">{events.error.message}</p>}
          {events.data?.map((event) => (
            <button className="event-card" key={event.id} onClick={() => setSelectedEvent(event)}>
              <strong>{event.title}</strong><span>{due(event.starts_at)}</span>
            </button>
          ))}
        </section>
      )}

      {taskComposerOpen && (
        <div
          className="sheet-backdrop"
          onMouseDown={(event) =>
            event.target === event.currentTarget && setTaskComposerOpen(false)
          }
        >
          <TaskComposer
            user={user}
            token={token}
            events={events.data ?? []}
            close={() => setTaskComposerOpen(false)}
          />
        </div>
      )}
      {eventComposerOpen && (
        <div
          className="sheet-backdrop"
          onMouseDown={(event) =>
            event.target === event.currentTarget && setEventComposerOpen(false)
          }
        >
          <EventComposer token={token} close={() => setEventComposerOpen(false)} />
        </div>
      )}
      {selected && (
        <div
          className="sheet-backdrop"
          onMouseDown={(event) => event.target === event.currentTarget && setSelected(null)}
        >
          <TaskSheet task={selected} user={user} token={token} close={() => setSelected(null)} />
        </div>
      )}
      {selectedEvent && (
        <div
          className="sheet-backdrop"
          onMouseDown={(event) => event.target === event.currentTarget && setSelectedEvent(null)}
        >
          <ArchiveSheet
            event={selectedEvent}
            user={user}
            token={token}
            close={() => setSelectedEvent(null)}
          />
        </div>
      )}
      {adminOpen && (
        <div
          className="sheet-backdrop"
          onMouseDown={(event) => event.target === event.currentTarget && setAdminOpen(false)}
        >
          <AdminSheet token={token} close={() => setAdminOpen(false)} />
        </div>
      )}
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={client}>
    <App />
  </QueryClientProvider>,
)
