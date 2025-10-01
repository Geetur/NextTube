'use client';

import { useEffect, useRef, useState } from 'react';
import Hls from 'hls.js';

export default function WatchPage({ params }: { params: { id: string } }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [status, setStatus] = useState<'idle'|'loading'|'playing'|'error'>('idle');
  const [startupMs, setStartupMs] = useState<number | null>(null);
  const [rebufferCount, setRebufferCount] = useState(0);

  useEffect(() => {
    const id = params.id;
    const src = `http://localhost:8000/videos/${id}/playlist`;

    const v = videoRef.current!;
    let firstPlayAt: number | null = null;
    const start = performance.now();

    const onPlaying = () => {
      if (firstPlayAt === null) {
        firstPlayAt = performance.now();
        setStartupMs(firstPlayAt - start);
        setStatus('playing');
      }
    };
    const onWaiting = () => setRebufferCount(c => c + 1);

    v.addEventListener('playing', onPlaying);
    v.addEventListener('waiting', onWaiting);

    setStatus('loading');

    if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: true });
      hls.loadSource(src);
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        console.log('hls error', data);
        if (data?.fatal) setStatus('error');
      });
      return () => {
        v.removeEventListener('playing', onPlaying);
        v.removeEventListener('waiting', onWaiting);
        hls.destroy();
      };
    } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
      v.src = src;
      v.play().catch(() => {});
      return () => {
        v.removeEventListener('playing', onPlaying);
        v.removeEventListener('waiting', onWaiting);
      };
    } else {
      setStatus('error');
    }
  }, [params.id]);

  return (
    <main style={{minHeight:'100vh', padding:'24px', fontFamily:'ui-sans-serif, system-ui'}}>
      <h1 style={{fontSize:'1.25rem', fontWeight:700}}>Watch: {params.id}</h1>
      <video
        ref={videoRef}
        controls
        playsInline
        style={{width:'100%', maxWidth: 900, background:'#000', borderRadius:12, marginTop:12}}
      />
      <div style={{opacity:0.8, marginTop:8}}>
        <div>Status: {status}</div>
        {startupMs !== null && <div>Startup: {Math.round(startupMs)} ms</div>}
        <div>Rebuffers: {rebufferCount}</div>
      </div>
    </main>
  );
}
