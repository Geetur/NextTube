import './globals.css';

export const metadata = {
  title: 'NextTube — watch what\'s next',
  description: 'Upload, transcode, and stream video with NextTube.',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
