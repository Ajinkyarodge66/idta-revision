/**
 * IDTA Revision: accounts & class activity backend (Google Apps Script)
 *
 * Sheets (created automatically):
 *   Users    one row per student account (password stored only as a salted, peppered hash)
 *   Sessions login tokens (stored only as hashes), 30-day expiry
 *   Events   activity rows (answers, mocks, Calc Lab)
 *
 * All requests are POST with a JSON body {action, ...}. GET ?action=ping is a health check.
 * Setup: backend/SETUP.md. Script properties: ADMIN_KEY (you set it), PEPPER (auto-created).
 */
const APP = 'IDTA Revision';
const SHEETS = {
  users:    { name: 'Users',    cols: ['ID', 'Created', 'Name', 'Mobile', 'Email', 'University', 'Course', 'Password hash', 'Salt', 'On leaderboard', 'Progress', 'Progress updated', 'Last login', 'Reset code', 'Reset expires', 'Consent', 'University email', 'Verified university', 'Verify code', 'Verify expires'] },
  sessions: { name: 'Sessions', cols: ['Token hash', 'User ID', 'Created', 'Expires'] },
  events:   { name: 'Events',   cols: ['Time', 'User ID', 'Name', 'Event', 'Topic', 'Question', 'Correct', 'Score', 'Out of', 'Detail'] },
  news:     { name: 'Announcements', cols: ['ID', 'Created', 'Title', 'Message', 'Level', 'Expires', 'Audience'] },
  questions:{ name: 'Questions', cols: ['ID', 'Updated', 'Topic', 'Type', 'Prompt', 'Options', 'Answer', 'Explanation', 'Audience'] }
};
const TOPIC_KEYS = ['data', 'dist', 'prep', 'cls', 'eval', 'nn', 'ens', 'reg', 'arm', 'clu', 'py', 'math', 'nlp', 'genai', 'ops'];
const U = { id: 0, created: 1, name: 2, mobile: 3, email: 4, uni: 5, course: 6, hash: 7, salt: 8, lb: 9, prog: 10, progAt: 11, last: 12, rcode: 13, rexp: 14, consent: 15, uniEmail: 16, vuni: 17, vcode: 18, vexp: 19 };
const DEFAULT_DOMAINS = { 'myport.ac.uk': 'University of Portsmouth', 'port.ac.uk': 'University of Portsmouth' };
const SESSION_DAYS = 30;
const MAX_PROGRESS_CHARS = 45000;

/* ---------- plumbing ---------- */
function book_() {
  let ss = null;
  try { ss = SpreadsheetApp.getActiveSpreadsheet(); } catch (err) {}
  if (ss) return ss;
  const p = PropertiesService.getScriptProperties();
  const id = p.getProperty('SHEET_ID');
  if (id) return SpreadsheetApp.openById(id);
  ss = SpreadsheetApp.create('IDTA Revision Data');
  p.setProperty('SHEET_ID', ss.getId());
  return ss;
}
function sheet_(key) {
  const def = SHEETS[key], ss = book_();
  let sh = ss.getSheetByName(def.name);
  if (!sh) {
    sh = ss.insertSheet(def.name);
    if (key === 'users' || key === 'sessions') sh.getRange(1, 1, 1000, def.cols.length).setNumberFormat('@'); // keep mobiles/ids as text
    sh.getRange(1, 1, 1, def.cols.length).setValues([def.cols]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}
function props_() { return PropertiesService.getScriptProperties(); }
function cache_() { return CacheService.getScriptCache(); }
function pepper_() {
  const p = props_();
  let v = p.getProperty('PEPPER');
  if (!v) { v = Utilities.getUuid() + Utilities.getUuid(); p.setProperty('PEPPER', v); }
  return v;
}
function sha_(s) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, String(s), Utilities.Charset.UTF_8)
    .map(b => ((b + 256) % 256).toString(16).padStart(2, '0')).join('');
}
// The browser already ran PBKDF2 (150k rounds); this adds a per-user salt and a server-only pepper.
function hashPw_(derived, salt) {
  let h = salt + '|' + derived + '|' + pepper_();
  for (let i = 0; i < 50; i++) h = sha_(h);
  return h;
}
function now_() { return new Date().toISOString(); }
function out_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
function fail_(error, extra) { return Object.assign({ ok: false, error: error }, extra || {}); }
function withLock_(fn) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return fail_('busy');
  try { return fn(); } finally { lock.releaseLock(); }
}
function text_(v, max) {
  const s = String(v == null ? '' : v).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, max || 60);
  return /^[=+\-@]/.test(s) ? "'" + s : s;   // stop spreadsheet formula injection
}
function plain_(v, max) { return String(v == null ? '' : v).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, max || 60); }  // for text stored inside JSON cells
function num_(v) { const n = Number(v); return v === null || v === undefined || v === '' || !isFinite(n) ? '' : n; }
function mobile_(v) { const d = String(v || '').replace(/\D/g, ''); return d.length >= 8 && d.length <= 15 ? d : ''; }
function email_(v) { const s = String(v || '').trim().toLowerCase(); return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(s) && s.length <= 100 ? s : ''; }
function pw_(v) { return /^[a-f0-9]{64}$/.test(String(v || '')) ? String(v) : ''; }
function pid_(id) { return sha_('pid|' + id).slice(0, 12); }

/* ---------- users & sessions ---------- */
function usersAll_() { const sh = sheet_('users'); return { sh: sh, v: sh.getDataRange().getValues() }; }
function rowOf_(v, col, val) { for (let i = 1; i < v.length; i++) if (String(v[i][col]) === String(val)) return i; return -1; }
function setCell_(sh, i, col, val) { sh.getRange(i + 1, col + 1).setValue(val); }
function profile_(r) {
  return { id: String(r[U.id]), pid: pid_(r[U.id]), name: String(r[U.name]), mobile: String(r[U.mobile]), email: String(r[U.email]),
    university: String(r[U.uni]), course: String(r[U.course]), lb: r[U.lb] === 'yes', created: String(r[U.created]),
    uniEmail: String(r[U.uniEmail] || ''), verifiedUni: String(r[U.vuni] || '') };
}
function progress_(r) { try { return r[U.prog] ? JSON.parse(r[U.prog]) : null; } catch (e) { return null; } }
function newSession_(uid) {
  const token = (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '');
  const exp = new Date(Date.now() + SESSION_DAYS * 864e5).toISOString();
  sheet_('sessions').appendRow([sha_(token), uid, now_(), exp]);
  cache_().put('tok:' + sha_(token), uid, 21600);
  return token;
}
function auth_(token) {
  if (!/^[a-f0-9]{64}$/.test(String(token || ''))) return null;
  const h = sha_(token), c = cache_().get('tok:' + h);
  if (c) return c;
  const v = sheet_('sessions').getDataRange().getValues();
  for (let i = 1; i < v.length; i++) {
    if (v[i][0] === h && new Date(v[i][3]).getTime() > Date.now()) { cache_().put('tok:' + h, String(v[i][1]), 21600); return String(v[i][1]); }
  }
  return null;
}
function dropRows_(sh, keepFn) {
  const v = sh.getDataRange().getValues();
  const keep = v.filter((r, i) => i === 0 || keepFn(r));
  if (keep.length === v.length) return 0;
  sh.clearContents();
  sh.getRange(1, 1, keep.length, v[0].length).setValues(keep);
  return v.length - keep.length;
}
function killSessions_(uid) {
  const sh = sheet_('sessions'), v = sh.getDataRange().getValues();
  v.slice(1).forEach(r => { if (String(r[1]) === uid) cache_().remove('tok:' + r[0]); });
  dropRows_(sh, r => String(r[1]) !== uid);
}
function tooMany_(key, limit) { const n = Number(cache_().get(key) || 0); return n >= limit; }
function bump_(key, secs) { const c = cache_(); c.put(key, String(Number(c.get(key) || 0) + 1), secs); }
function me_(b) {
  const uid = auth_(b.token);
  if (!uid) return null;
  const all = usersAll_(), i = rowOf_(all.v, U.id, uid);
  return i < 0 ? null : { uid: uid, i: i, sh: all.sh, r: all.v[i] };
}

/* ---------- API ---------- */
const API = {
  ping: () => ({ ok: true, app: APP }),

  signup: b => {
    const name = text_(b.name, 40), mobile = mobile_(b.mobile), email = email_(b.email);
    const uni = text_(b.university, 80), course = text_(b.course, 80), pw = pw_(b.pw);
    if (name.length < 2) return fail_('bad_name');
    if (!mobile) return fail_('bad_mobile');
    if (!email) return fail_('bad_email');
    if (uni.length < 2) return fail_('bad_university');
    if (course.length < 2) return fail_('bad_course');
    if (!pw) return fail_('bad_password');
    if (b.consent !== true) return fail_('no_consent');
    return withLock_(() => {
      const all = usersAll_();
      if (rowOf_(all.v, U.mobile, mobile) > 0) return fail_('mobile_taken');
      if (rowOf_(all.v, U.email, email) > 0) return fail_('email_taken');
      const id = Utilities.getUuid(), salt = Utilities.getUuid();
      const row = [id, now_(), name, mobile, email, uni, course, hashPw_(pw, salt), salt, b.lb === false ? 'no' : 'yes', '', '', now_(), '', '', now_()];
      all.sh.appendRow(row);
      cache_().remove('leaderboard');
      return { ok: true, token: newSession_(id), profile: profile_(row), progress: null };
    });
  },

  login: b => {
    const mobile = mobile_(b.mobile), pw = pw_(b.pw);
    if (!mobile || !pw) return fail_('wrong_login');
    const key = 'fail:' + mobile;
    if (tooMany_(key, 5)) return fail_('too_many_attempts');
    const all = usersAll_(), i = rowOf_(all.v, U.mobile, mobile);
    if (i < 0 || hashPw_(pw, all.v[i][U.salt]) !== all.v[i][U.hash]) { bump_(key, 900); return fail_('wrong_login'); }
    cache_().remove(key);
    setCell_(all.sh, i, U.last, now_());
    return { ok: true, token: newSession_(String(all.v[i][U.id])), profile: profile_(all.v[i]), progress: progress_(all.v[i]) };
  },

  me: b => {
    const m = me_(b); if (!m) return fail_('auth');
    return { ok: true, profile: profile_(m.r), progress: progress_(m.r) };
  },

  logout: b => {
    if (!/^[a-f0-9]{64}$/.test(String(b.token || ''))) return { ok: true };
    const h = sha_(b.token);
    cache_().remove('tok:' + h);
    withLock_(() => dropRows_(sheet_('sessions'), r => r[0] !== h));
    return { ok: true };
  },

  saveProgress: b => {
    const s = JSON.stringify(b.progress || {});
    if (s.length > MAX_PROGRESS_CHARS) return fail_('progress_too_big');
    return withLock_(() => {
      const m = me_(b); if (!m) return fail_('auth');
      setCell_(m.sh, m.i, U.prog, s);
      setCell_(m.sh, m.i, U.progAt, now_());
      return { ok: true };
    });
  },

  events: b => {
    const m = me_(b); if (!m) return fail_('auth');
    const list = Array.isArray(b.events) ? b.events.slice(0, 300) : [];
    if (!list.length) return { ok: true, saved: 0 };
    const name = String(m.r[U.name]);
    const rows = list.map(ev => [
      ev && ev.ts ? new Date(Number(ev.ts)) : new Date(), m.uid, name,
      text_(ev.t, 10), text_(ev.topic, 20), num_(ev.id),
      ev.ok === true ? 1 : ev.ok === false ? 0 : '', num_(ev.s), num_(ev.n), text_(ev.k, 30)
    ]);
    return withLock_(() => {
      const sh = sheet_('events');
      sh.getRange(sh.getLastRow() + 1, 1, rows.length, SHEETS.events.cols.length).setValues(rows);
      return { ok: true, saved: rows.length };
    });
  },

  updateProfile: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const name = text_(b.name, 40), uni = text_(b.university, 80), course = text_(b.course, 80);
    if (name.length < 2) return fail_('bad_name');
    if (uni.length < 2) return fail_('bad_university');
    if (course.length < 2) return fail_('bad_course');
    setCell_(m.sh, m.i, U.name, name); setCell_(m.sh, m.i, U.uni, uni); setCell_(m.sh, m.i, U.course, course);
    setCell_(m.sh, m.i, U.lb, b.lb === false ? 'no' : 'yes');
    cache_().remove('leaderboard');
    const r = m.r.slice(); r[U.name] = name; r[U.uni] = uni; r[U.course] = course; r[U.lb] = b.lb === false ? 'no' : 'yes';
    return { ok: true, profile: profile_(r) };
  }),

  changePassword: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const oldPw = pw_(b.oldPw), newPw = pw_(b.newPw);
    if (!oldPw || hashPw_(oldPw, m.r[U.salt]) !== m.r[U.hash]) return fail_('wrong_password');
    if (!newPw) return fail_('bad_password');
    const salt = Utilities.getUuid();
    setCell_(m.sh, m.i, U.salt, salt); setCell_(m.sh, m.i, U.hash, hashPw_(newPw, salt));
    killSessions_(m.uid);
    return { ok: true, token: newSession_(m.uid) };
  }),

  forgot: b => {
    const mobile = mobile_(b.mobile);
    if (!mobile) return fail_('bad_mobile');
    const key = 'forgot:' + mobile;
    if (tooMany_(key, 3)) return fail_('too_many_attempts');
    bump_(key, 3600);
    return withLock_(() => {
      const all = usersAll_(), i = rowOf_(all.v, U.mobile, mobile);
      if (i > 0) {
        const code = String(100000 + (parseInt(sha_(Utilities.getUuid()).slice(0, 8), 16) % 900000));
        setCell_(all.sh, i, U.rcode, sha_(code + '|' + all.v[i][U.salt]));
        setCell_(all.sh, i, U.rexp, new Date(Date.now() + 15 * 60000).toISOString());
        MailApp.sendEmail({
          to: String(all.v[i][U.email]),
          subject: APP + ': your password reset code',
          body: 'Hi ' + all.v[i][U.name] + ',\n\nYour IDTA Revision password reset code is: ' + code +
                '\n\nIt expires in 15 minutes. If you did not ask for this, ignore this email; your password has not changed.'
        });
      }
      return { ok: true };  // same answer whether or not the number exists
    });
  },

  reset: b => {
    const mobile = mobile_(b.mobile), code = String(b.code || '').replace(/\D/g, ''), pw = pw_(b.pw);
    if (!mobile || code.length !== 6) return fail_('bad_code');
    if (!pw) return fail_('bad_password');
    const key = 'resetfail:' + mobile;
    if (tooMany_(key, 5)) return fail_('too_many_attempts');
    return withLock_(() => {
      const all = usersAll_(), i = rowOf_(all.v, U.mobile, mobile);
      const r = i > 0 ? all.v[i] : null;
      if (!r || !r[U.rcode] || new Date(r[U.rexp]).getTime() < Date.now() || sha_(code + '|' + r[U.salt]) !== r[U.rcode]) {
        bump_(key, 900); return fail_('bad_code');
      }
      const salt = Utilities.getUuid();
      setCell_(all.sh, i, U.salt, salt); setCell_(all.sh, i, U.hash, hashPw_(pw, salt));
      setCell_(all.sh, i, U.rcode, ''); setCell_(all.sh, i, U.rexp, '');
      cache_().remove(key); cache_().remove('fail:' + mobile);
      const uid = String(r[U.id]);
      killSessions_(uid);
      return { ok: true, token: newSession_(uid), profile: profile_(r), progress: progress_(r) };
    });
  },

  deleteAccount: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const pw = pw_(b.pw);
    if (!pw || hashPw_(pw, m.r[U.salt]) !== m.r[U.hash]) return fail_('wrong_password');
    killSessions_(m.uid);
    dropRows_(sheet_('events'), r => String(r[1]) !== m.uid);
    dropRows_(m.sh, r => String(r[U.id]) !== m.uid);
    cache_().remove('leaderboard');
    return { ok: true };
  }),

  // Public: announcements, admin-added questions and hidden built-in question ids. Cached for 60 s.
  config: () => {
    const hit = cache_().get('config');
    if (hit) return JSON.parse(hit);
    const now = Date.now();
    const news = sheet_('news').getDataRange().getValues().slice(1)
      .filter(r => r[0] && !r[6] && (!r[5] || new Date(r[5]).getTime() > now))
      .map(r => ({ id: String(r[0]), created: String(r[1]), title: String(r[2]), message: String(r[3]), level: String(r[4]) || 'info' }))
      .reverse();
    const questions = sheet_('questions').getDataRange().getValues().slice(1).filter(r => r[0] !== '' && !r[8]).map(qOut_).filter(Boolean);
    let hidden = [];
    try { hidden = JSON.parse(props_().getProperty('HIDDEN_Q') || '[]'); } catch (e) {}
    const out = { ok: true, news: news, questions: questions, hidden: hidden, unis: unis_(), at: now };
    const str = JSON.stringify(out);
    if (str.length < 90000) cache_().put('config', str, 60);
    return out;
  },

  adminAnnounce: b => admin_(b, () => {
    const title = text_(b.title, 80), message = text_(b.message, 600), level = b.level === 'important' ? 'important' : 'info', aud = audience_(b.audience);
    if (aud === null) return fail_('bad_audience');
    const days = Math.max(1, Math.min(60, Number(b.days) || 7));
    if (title.length < 2) return fail_('bad_title');
    const id = Utilities.getUuid().slice(0, 8);
    sheet_('news').appendRow([id, now_(), title, message, level, new Date(Date.now() + days * 864e5).toISOString(), aud]);
    cache_().remove('config');
    return { ok: true, id: id };
  }),
  adminDeleteAnnouncement: b => admin_(b, () => {
    dropRows_(sheet_('news'), r => String(r[0]) !== String(b.id));
    cache_().remove('config');
    return { ok: true };
  }),
  adminSaveQuestion: b => admin_(b, () => {
    const q = qIn_(b.question);
    if (q.error) return fail_(q.error);
    const aud = audience_(b.question.audience);
    if (aud === null) return fail_('bad_audience');
    const sh = sheet_('questions'), v = sh.getDataRange().getValues();
    let id = Number(b.question.id);
    const row = (qid) => [qid, now_(), q.t, q.type, q.p, JSON.stringify(q.o), JSON.stringify(q.a), q.e, aud];
    const i = id ? rowOf_(v, 0, id) : -1;
    if (i > 0) sh.getRange(i + 1, 1, 1, 9).setValues([row(id)]);
    else {
      id = Math.max(999, ...v.slice(1).map(r => Number(r[0]) || 0)) + 1;
      sh.appendRow(row(id));
    }
    cache_().remove('config');
    return { ok: true, id: id };
  }),
  adminDeleteQuestion: b => admin_(b, () => {
    dropRows_(sheet_('questions'), r => Number(r[0]) !== Number(b.id));
    cache_().remove('config');
    return { ok: true };
  }),
  adminHideBuiltin: b => admin_(b, () => {
    let hidden = [];
    try { hidden = JSON.parse(props_().getProperty('HIDDEN_Q') || '[]'); } catch (e) {}
    const id = Number(b.id);
    if (!(id >= 0 && id < 1000)) return fail_('bad_question');
    hidden = hidden.filter(x => x !== id);
    if (b.hide) hidden.push(id);
    props_().setProperty('HIDDEN_Q', JSON.stringify(hidden));
    cache_().remove('config');
    return { ok: true, hidden: hidden };
  }),

  /* ----- university verification & private packs ----- */
  verifyStart: b => {
    const email = email_(b.email);
    if (!email) return fail_('bad_email');
    const uni = domains_()[email.split('@')[1]];
    if (!uni) return fail_('unknown_domain');
    return withLock_(() => {
      const m = me_(b); if (!m) return fail_('auth');
      const key = 'verify:' + m.uid;
      if (tooMany_(key, 4)) return fail_('too_many_attempts');
      const all = usersAll_(), other = rowOf_(all.v, U.uniEmail, email);
      if (other > 0 && other !== m.i && all.v[other][U.vuni]) return fail_('email_used');
      bump_(key, 3600);
      const code = String(100000 + (parseInt(sha_(Utilities.getUuid()).slice(0, 8), 16) % 900000));
      setCell_(m.sh, m.i, U.uniEmail, email);
      setCell_(m.sh, m.i, U.vcode, sha_(code + '|' + m.r[U.salt]));
      setCell_(m.sh, m.i, U.vexp, new Date(Date.now() + 15 * 60000).toISOString());
      MailApp.sendEmail({ to: email, subject: APP + ': verify your university email',
        body: 'Hi ' + m.r[U.name] + ',\n\nYour code to verify your ' + uni + ' email is: ' + code + '\n\nIt expires in 15 minutes. If you did not request this, you can ignore this email.' });
      return { ok: true, university: uni };
    });
  },
  verifyConfirm: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const key = 'vfail:' + m.uid;
    if (tooMany_(key, 5)) return fail_('too_many_attempts');
    const code = String(b.code || '').replace(/\D/g, ''), r = m.r;
    if (code.length !== 6 || !r[U.vcode] || new Date(r[U.vexp]).getTime() < Date.now() || sha_(code + '|' + r[U.salt]) !== r[U.vcode]) { bump_(key, 900); return fail_('bad_code'); }
    const uni = domains_()[String(r[U.uniEmail]).split('@')[1]];
    if (!uni) return fail_('unknown_domain');
    setCell_(m.sh, m.i, U.vuni, uni); setCell_(m.sh, m.i, U.vcode, ''); setCell_(m.sh, m.i, U.vexp, '');
    cache_().remove('leaderboard');
    const row = r.slice(); row[U.vuni] = uni;
    return { ok: true, profile: profile_(row) };
  }),
  pack: b => {
    const m = me_(b); if (!m) return fail_('auth');
    const uni = String(m.r[U.vuni] || '');
    if (!uni) return fail_('not_verified');
    const pk = packs_()[uni] || {};
    const now = Date.now();
    const extra = sheet_('questions').getDataRange().getValues().slice(1).filter(r => r[0] !== '' && String(r[8]) === uni).map(qOut_).filter(Boolean);
    const news = sheet_('news').getDataRange().getValues().slice(1).filter(r => r[0] && String(r[6]) === uni && (!r[5] || new Date(r[5]).getTime() > now))
      .map(r => ({ id: String(r[0]), created: String(r[1]), title: String(r[2]), message: String(r[3]), level: String(r[4]) || 'info' })).reverse();
    return { ok: true, uni: uni, title: pk.title || '', exam: pk.exam || null, notes: pk.notes || '', questions: pk.questions || [], extra: extra, news: news };
  },
  adminContent: b => admin_(b, () => ({
    ok: true,
    questions: sheet_('questions').getDataRange().getValues().slice(1).filter(r => r[0] !== '').map(qOut_).filter(Boolean),
    news: sheet_('news').getDataRange().getValues().slice(1).filter(r => r[0] && (!r[5] || new Date(r[5]).getTime() > Date.now()))
      .map(r => ({ id: String(r[0]), created: String(r[1]), title: String(r[2]), message: String(r[3]), level: String(r[4]) || 'info', audience: String(r[6] || '') })).reverse()
  })),
  // Grant or revoke a student's private university pack by hand (uni = '' revokes).
  adminSetAccess: b => admin_(b, () => {
    const all = usersAll_(), i = rowOf_(all.v, U.id, String(b.id || ''));
    if (i < 0) return fail_('no_user');
    const uni = String(b.uni || '');
    if (uni && !unis_().some(u => u.name === uni)) return fail_('bad_university');
    setCell_(all.sh, i, U.vuni, uni);
    if (!uni) { setCell_(all.sh, i, U.vcode, ''); setCell_(all.sh, i, U.vexp, ''); }
    cache_().remove('leaderboard');
    return { ok: true, id: String(all.v[i][U.id]), verifiedUni: uni };
  }),
  adminSetDomains: b => admin_(b, () => {
    if (b.domains && typeof b.domains === 'object') {
      const clean = {};
      Object.keys(b.domains).slice(0, 50).forEach(d => {
        const dom = String(d).toLowerCase().replace(/^@/, '').trim(), name = plain_(b.domains[d], 80);
        if (/^[a-z0-9.-]+\.[a-z]{2,}$/.test(dom) && name.length >= 3) clean[dom] = name;
      });
      props_().setProperty('UNI_DOMAINS', JSON.stringify(clean));
      cache_().remove('config');
    }
    return { ok: true, domains: domains_(), packs: Object.keys(packs_()) };
  }),

  leaderboard: () => {
    const hit = cache_().get('leaderboard');
    if (hit) return JSON.parse(hit);
    const s = stats_();
    const users = usersAll_().v.slice(1);
    const rows = users.filter(r => r[U.lb] === 'yes').map(r => {
      const st = s.users[String(r[U.id])] || blank_();
      return { pid: pid_(r[U.id]), name: String(r[U.name]), course: String(r[U.course]), university: String(r[U.vuni] || r[U.uni] || ''), verified: !!r[U.vuni], best: st.best, mocks: st.mocks, q: st.q, acc: st.q ? Math.round(st.qr / st.q * 100) : null };
    });
    const out = { ok: true, players: users.length, topics: s.topics, leaderboard: rows };
    const str = JSON.stringify(out);
    if (str.length < 90000) cache_().put('leaderboard', str, 60);
    return out;
  },

  admin: b => {
    const key = props_().getProperty('ADMIN_KEY');
    if (!key || String(b.key || '') !== key) return fail_('bad_key');
    const s = stats_(), users = usersAll_().v.slice(1);
    return {
      ok: true, rows: s.rows, topics: s.topics, questions: s.questions, recent: s.recent,
      users: users.map(r => Object.assign({
        id: String(r[U.id]), name: String(r[U.name]), mobile: String(r[U.mobile]), email: String(r[U.email]),
        university: String(r[U.uni]), course: String(r[U.course]), lb: r[U.lb] === 'yes', created: String(r[U.created]), lastLogin: String(r[U.last]),
        uniEmail: String(r[U.uniEmail] || ''), verifiedUni: String(r[U.vuni] || '')
      }, s.users[String(r[U.id])] || blank_()))
    };
  }
};
function domains_() { try { return JSON.parse(props_().getProperty('UNI_DOMAINS') || 'null') || DEFAULT_DOMAINS; } catch (e) { return DEFAULT_DOMAINS; } }
function packs_() { return typeof PACKS !== 'undefined' ? PACKS : {}; }   // PACKS lives in Pack.gs (kept private, never in the public repo)
function unis_() {
  const d = domains_(), by = {}, pk = packs_();
  Object.keys(d).forEach(dom => { (by[d[dom]] = by[d[dom]] || []).push(dom); });
  return Object.keys(by).map(n => ({ name: n, domains: by[n], pack: !!pk[n] }));
}
function audience_(a) { a = String(a || '').trim(); if (!a) return ''; return unis_().some(u => u.name === a) ? a : null; }
function admin_(b, fn) {
  const key = props_().getProperty('ADMIN_KEY');
  if (!key || String(b.key || '') !== key) return fail_('bad_key');
  return withLock_(fn);
}
// Validate an admin-written question. Shapes match the app: single {a: index}, multi {a: [indexes]}, match {l: [...], r: [...]}.
function qIn_(q) {
  q = q || {};
  const t = TOPIC_KEYS.indexOf(q.t) >= 0 ? q.t : null, type = ['single', 'multi', 'match'].indexOf(q.type) >= 0 ? q.type : null;
  const p = text_(q.p, 600), e = text_(q.e, 600);
  if (!t) return { error: 'bad_topic' };
  if (!type) return { error: 'bad_type' };
  if (p.length < 5) return { error: 'bad_prompt' };
  if (type === 'match') {
    const l = (q.l || []).map(x => plain_(x, 160)).filter(Boolean), r = (q.r || []).map(x => plain_(x, 160)).filter(Boolean);
    if (l.length < 2 || l.length !== r.length || l.length > 8) return { error: 'bad_pairs' };
    return { t: t, type: type, p: p, o: { l: l, r: r }, a: null, e: e };
  }
  const o = (q.o || []).map(x => plain_(x, 200)).filter(Boolean);
  if (o.length < 2 || o.length > 8) return { error: 'bad_options' };
  if (type === 'single') { const a = Number(q.a); if (!(a >= 0 && a < o.length)) return { error: 'bad_answer' }; return { t: t, type: type, p: p, o: o, a: a, e: e }; }
  const a = [...new Set((q.a || []).map(Number))].filter(x => x >= 0 && x < o.length);
  if (!a.length) return { error: 'bad_answer' };
  return { t: t, type: type, p: p, o: o, a: a, e: e };
}
function qOut_(r) {
  try {
    const o = JSON.parse(r[5]), a = JSON.parse(r[6]);
    const q = { id: Number(r[0]), t: String(r[2]), type: String(r[3]), p: String(r[4]).replace(/^'/, ''), e: String(r[7]).replace(/^'/, ''), custom: true, audience: String(r[8] || '') };
    if (q.type === 'match') { q.l = o.l; q.r = o.r; } else { q.o = o; q.a = a; }
    return q;
  } catch (e) { return null; }
}
function blank_() { return { last: 0, q: 0, qr: 0, mocks: 0, best: null, lastMock: '', lab: 0, labr: 0, today: 0 }; }
function stats_() {
  const v = sheet_('events').getDataRange().getValues().slice(1);
  const users = {}, topics = {}, questions = {}, dayAgo = Date.now() - 864e5;
  v.forEach(r => {
    const ts = new Date(r[0]).getTime(), uid = String(r[1]), ev = r[3], topic = r[4], qid = r[5], ok = r[6], s = r[7], n = r[8];
    if (!uid) return;
    const u = users[uid] = users[uid] || blank_();
    if (ts > u.last) u.last = ts;
    if (ts > dayAgo) u.today++;
    if (ev === 'q') {
      u.q++; if (ok === 1) u.qr++;
      if (topic) { const t = topics[topic] = topics[topic] || [0, 0]; t[1]++; if (ok === 1) t[0]++; }
      if (qid !== '') { const q = questions[qid] = questions[qid] || [0, 0]; q[1]++; if (ok === 1) q[0]++; }
    } else if (ev === 'mock') {
      u.mocks++; u.lastMock = s + '/' + n;
      if (Number(n) === 30 && (u.best === null || Number(s) > u.best)) u.best = Number(s);
    } else if (ev === 'lab') { u.lab++; if (ok === 1) u.labr++; }
  });
  return {
    rows: v.length, users: users, topics: topics, questions: questions,
    recent: v.slice(-200).reverse().map(r => ({ ts: new Date(r[0]).getTime(), name: r[2], ev: r[3], topic: r[4], qid: r[5], ok: r[6], s: r[7], n: r[8], k: r[9] }))
  };
}

/* ---------- entry points ---------- */
function doPost(e) {
  let b;
  try { b = JSON.parse(e.postData.contents); } catch (err) { return out_(fail_('bad_request')); }
  const fn = API[b && b.action];
  if (!fn) return out_(fail_('unknown_action'));
  try { return out_(fn(b)); }
  catch (err) { console.error(b.action, err); return out_(fail_('server_error')); }
}
function doGet(e) {
  const p = (e && e.parameter) || {};
  try { return out_(p.action === 'leaderboard' ? API.leaderboard() : p.action === 'config' ? API.config() : API.ping()); }
  catch (err) { return out_(fail_('server_error')); }
}

/* Run once from the editor: creates the sheets, the pepper, and asks for Gmail permission (for reset codes). */
function setup() {
  ['users', 'sessions', 'events', 'news', 'questions'].forEach(k => { const sh = sheet_(k); sh.getRange(1, 1, 1, SHEETS[k].cols.length).setValues([SHEETS[k].cols]).setFontWeight('bold'); });
  if (!props_().getProperty('UNI_DOMAINS')) props_().setProperty('UNI_DOMAINS', JSON.stringify(DEFAULT_DOMAINS));
  Logger.log('University packs installed: ' + (Object.keys(packs_()).join(', ') || 'none (add Pack.gs)'));
  pepper_();
  Logger.log('Data sheet ready: ' + book_().getUrl());
  Logger.log('Emails left today (for reset codes): ' + MailApp.getRemainingDailyQuota());
  Logger.log(props_().getProperty('ADMIN_KEY') ? 'ADMIN_KEY is set.' : 'ADMIN_KEY is NOT set: run setAdminKey first.');
}

/* Removes expired sessions. Optional: add a daily time trigger for this. */
function cleanSessions() {
  withLock_(() => dropRows_(sheet_('sessions'), r => new Date(r[3]).getTime() > Date.now()));
}
