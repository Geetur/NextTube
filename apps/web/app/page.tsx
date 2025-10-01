'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

type Video = { id: string; key: string; created_at: string };

export default function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [videos, setVideos] = useState<Video[]>([]);
  const router = useRouter();

  // load recent uploads
  useEffect(() => {
    fetch('http://localhost:8000/videos')
      .then(r => r.json())
      .then(setVideos)
      .catch(() => {});
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

  return (
    <main style={{minHeight:'100vh', fontFamily:'ui-sans-serif, system-ui', padding:'24px'}}>
      <h1 style={{fontSize:'1.5rem', fontWeight:700, marginBottom:12}}>Media Optimizer</h1>

      <form onSubmit={onUpload} style={{display:'flex', gap:8, alignItems:'center', marginBottom:20}}>
        <input type="file" accept="video/*" onChange={e => setFile(e.target.files?.[0] || null)} />
        <button
          disabled={!file || busy}
          style={{padding:'10px 14px', borderRadius:8, border:'1px solid #222', background:'#222', color:'#fff'}}
        >
          {busy ? 'Uploading…' : 'Upload & Transcode'}
        </button>
      </form>

      <h2 style={{fontSize:'1.1rem', fontWeight:600, margin:'8px 0'}}>Recent uploads</h2>
      <ul style={{display:'grid', gap:8, padding:0, listStyle:'none', maxWidth:800}}>
        {videos.map(v => (
          <li key={v.id} style={{display:'flex', justifyContent:'space-between', padding:'10px 12px', border:'1px solid #eee', borderRadius:10}}>
            <div>
              <div style={{fontWeight:600}}>{v.id}</div>
              <div style={{opacity:0.7, fontSize:12}}>{v.created_at}</div>
            </div>
            <button
              onClick={() => router.push(`/watch/${v.id}`)}
              style={{padding:'8px 12px', borderRadius:8, border:'1px solid #222', background:'#fff'}}
            >
              Watch
            </button>
          </li>
        ))}
        {videos.length === 0 && <li style={{opacity:0.7}}>No uploads yet.</li>}
      </ul>
    </main>
  );
}
