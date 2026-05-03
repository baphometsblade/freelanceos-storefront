/**
 * FreelanceOS Email Capture Worker
 * Stores leads in D1 database, CORS-enabled for GitHub Pages
 */

const ALLOWED_ORIGIN = 'https://baphometsblade.github.io';

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    const isAllowed = origin.startsWith(ALLOWED_ORIGIN) || origin.includes('localhost');

    const corsHeaders = {
      'Access-Control-Allow-Origin': isAllowed ? origin : ALLOWED_ORIGIN,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405, headers: corsHeaders });
    }

    let email, page, source;
    try {
      const body = await request.json();
      email = body.email?.trim().toLowerCase();
      page = body.page || 'unknown';
      source = body.source || 'storefront';
    } catch {
      return new Response(JSON.stringify({ error: 'Invalid JSON' }), {
        status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      });
    }

    if (!email || !email.includes('@')) {
      return new Response(JSON.stringify({ error: 'Invalid email' }), {
        status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      });
    }

    try {
      await env.DB.prepare(
        'INSERT OR IGNORE INTO leads (email, page, source) VALUES (?, ?, ?)'
      ).bind(email, page, source).run();

      return new Response(JSON.stringify({ success: true, message: 'Thanks! Check your inbox for FLASH40.' }), {
        status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: 'Database error' }), {
        status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      });
    }
  }
};
