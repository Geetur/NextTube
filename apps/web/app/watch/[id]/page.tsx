'use client';

import Hls from 'hls.js';
import { useEffect, useRef, useState } from 'react';

export default function WatchPage({ params }: { params: { id: string } }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'playing' | 'error'>('idle');
  const [startupMs, setStartupMs] = useState<number | null>(null);
  const [rebufferCount, setRebufferCount] = useState(0);

  useEffect(() => {
    const id = params.id;
    const src = `http://localhost:8000/videos/${id}/playlist?v=${Date.now()}`;
    const basicSrc = `http://localhost:8000/videos/${id}/basic`;

    const v = videoRef.current!;
    let firstPlayAt: number | null = null;
    let fallbackActive = false;
    let hls: Hls | null = null;
    const start = performance.now();

    const onPlaying = () => {
      if (firstPlayAt === null) {
        firstPlayAt = performance.now();
        setStartupMs(firstPlayAt - start);
        setStatus('playing');
      }
    };
    const onWaiting = () => setRebufferCount(c => c + 1);
    const playFallback = () => {
      if (fallbackActive) {
        setStatus('error');
        return;
      }
      fallbackActive = true;
      hls?.destroy();
      v.src = basicSrc;
      v.load();
      v.play().catch(() => setStatus('error'));
    };
    const onMediaError = () => playFallback();

    v.addEventListener('playing', onPlaying);
    v.addEventListener('waiting', onWaiting);
    v.addEventListener('error', onMediaError);

    setStatus('loading');

    if (Hls.isSupported()) {
      hls = new Hls({ enableWorker: true });
      hls.loadSource(src);
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => { }));
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        console.log('hls error', data);
        if (data?.fatal) playFallback();
      });
      return () => {
        v.removeEventListener('playing', onPlaying);
        v.removeEventListener('waiting', onWaiting);
        v.removeEventListener('error', onMediaError);
        hls.destroy();
      };
    } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
      v.src = src;
      v.play().catch(() => { });
      return () => {
        v.removeEventListener('playing', onPlaying);
        v.removeEventListener('waiting', onWaiting);
        v.removeEventListener('error', onMediaError);
      };
    } else {
      setStatus('error');
    }
  }, [params.id]);

  return (
    <main className="page">
      <nav className="nav">
        <span className="brand">
          <span className="brand-mark" aria-hidden>▶</span>
          <span className="brand-word">NextTube</span>
        </span>
        <a className="back-link" href="/">← All videos</a>
      </nav>

      <div className={`badge badge-${status}`}>
        <span className="badge-dot" />
        {status}
      </div>
      <h1 className="watch-title">Now playing</h1>
      <div className="watch-id">{params.id}</div>

      <div className="player-frame">
        <video ref={videoRef} controls playsInline />
      </div>

      <div className="stat-grid">
        <div className="card stat-card">
          <div className="stat-label">Startup</div>
          <div className="stat-value">{startupMs !== null ? `${Math.round(startupMs)} ms` : '—'}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Rebuffers</div>
          <div className="stat-value">{rebufferCount}</div>
        </div>
      </div>
    </main>
  );
}
