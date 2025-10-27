import './global.css';
import { RootProvider } from 'fumadocs-ui/provider';
import { Inter } from 'next/font/google';
import type { ReactNode } from 'react';
import { PHProvider, PostHogPageView } from '@/providers/posthog-provider';
import { AnalyticsTracker } from '@/components/analytics-tracker';
import { CookieConsent } from '@/components/cookie-consent';
import { Footer } from '@/components/footer';
import { Suspense } from 'react';

const inter = Inter({
  subsets: ['latin'],
});

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={inter.className} suppressHydrationWarning>
      <head>
        <link rel="icon" href="/docs/favicon.ico" sizes="any" />
      </head>
      <body className="flex min-h-screen flex-col">
        <PHProvider>
          <Suspense fallback={null}>
            <PostHogPageView />
          </Suspense>
          <AnalyticsTracker />
          <RootProvider search={{ options: { api: '/docs/api/search' } }}>{children}</RootProvider>
          <Footer />
          <CookieConsent />
        </PHProvider>
      </body>
    </html>
  );
}
