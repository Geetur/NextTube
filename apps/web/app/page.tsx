'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

type Video = { id: string; key: string; created_at: string };

export default function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [videos, setVideos] = useState<Video[]>([]);
  const router = useRouter();

  // load recent uploads
  useEffect(() => {
    fetch('http://localhost:8000/videos')
      .then(r => r.json())
      .then(setVideos)
      .catch(() => { });
  }, []);

  async function onUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;

    try {
      setBusy(true);

      // 1) upload
      const fd = new FormData();
      fd.append('file', file);
      const up = await fetch('http://localhost:8000/upload', { method: 'POST', body: fd });
      if (!up.ok) throw new Error('upload failed');
      const { video_id } = await up.json();

      // 2) transcode job
      const job = await fetch('http://localhost:8000/jobs/transcode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_id }),
      });
      if (!job.ok) throw new Error('job failed');

      // 3) go watch
      router.push(`/watch/${video_id}`);
    } catch (err) {
      alert((err as Error).message || 'Something went wrong');
    } finally {
      setBusy(false);
    }
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragActive(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  }

  return (
    <main className="page">
      <nav className="nav">
        <span className="brand">
          <span className="brand-mark" aria-hidden>▶</span>
          <span className="brand-word">NextTube</span>
        </span>
      </nav>
      <div className="chevron-rule" aria-hidden>
        <span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span><span>›</span>
      </div>

      <section className="hero">
        <h1>
          Upload today.
          <br />
          Watch what&rsquo;s <em>next</em>.
        </h1>
        <p>Drop in a video, we transcode it to HLS, and it&rsquo;s ready to stream in seconds.</p>
      </section>

      <form onSubmit={onUpload}>
        <div
          className={`card upload-zone${dragActive ? ' drag-active' : ''}`}
          onDragOver={e => { e.preventDefault(); setDragActive(true); }}
          onDragLeave={() => setDragActive(false)}
          onDrop={onDrop}
        >
          <div className="upload-row">
            <label className="file-picker">
              📼 Choose a video
              <input type="file" accept="video/*" onChange={e => setFile(e.target.files?.[0] || null)} />
            </label>
            <span className="file-name">{file ? file.name : 'or drag one in here'}</span>
          </div>
          <div className="upload-row">
            <button className="btn btn-primary" disabled={!file || busy}>
              {busy ? <span className="spinner" /> : null}
              {busy ? 'Uploading…' : 'Upload & Transcode'}
              {!busy && <span className="btn-arrow">→</span>}
            </button>
            <span className="hint">MP4, MOV, MKV — we handle the rest.</span>
          </div>
        </div>
      </form>

      <h2 className="section-title">Recent uploads</h2>
      <ul className="video-list">
        {videos.map((v, i) => (
          <li key={v.id} className="card video-card" style={{ animationDelay: `${i * 0.05}s` }}>
            <div>
              <div className="video-id">{v.id}</div>
              <div className="video-meta">{v.created_at}</div>
            </div>
            <button className="btn btn-ghost" onClick={() => router.push(`/watch/${v.id}`)}>
              Watch <span className="btn-arrow">→</span>
            </button>
          </li>
        ))}
        {videos.length === 0 && (
          <li className="card empty-state">
            <span className="emoji">🎬</span>
            Nothing here yet — upload your first video to get rolling.
          </li>
        )}
      </ul>
    </main>
  );
}
