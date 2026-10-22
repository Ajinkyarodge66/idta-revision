/**
 * IDTA Revision: accounts & class activity backend (Google Apps Script)
 *
 * Sheets (created automatically):
 *   Users    one row per student account (password stored only as a salted, peppered hash)
 *   Sessions login tokens (stored only as hashes), 30-day expiry
 *   Events   activity rows (answers, mocks, Calc Lab)
 *
 * All requests are POST with a JSON body {action, ...}. GET ?action=ping is a health check.
 * Setup: backend/SETUP.md. Script properties: ADMIN_KEY (you set it), PEPPER (auto-created), EXAM_CFG (set from Admin).
 */
const APP = 'IDTA Revision';
const SHEETS = {
  users:    { name: 'Users',    cols: ['ID', 'Created', 'Name', 'Mobile', 'Email', 'University', 'Course', 'Password hash', 'Salt', 'On leaderboard', 'Progress', 'Progress updated', 'Last login', 'Reset code', 'Reset expires', 'Consent', 'University email', 'Verified university', 'Verify code', 'Verify expires', 'Exam access', 'Exam first used', 'Referred by', 'Source', 'Device'] },
  sessions: { name: 'Sessions', cols: ['Token hash', 'User ID', 'Created', 'Expires'] },
  events:   { name: 'Events',   cols: ['Time', 'User ID', 'Name', 'Event', 'Topic', 'Question', 'Correct', 'Score', 'Out of', 'Detail'] },
  news:     { name: 'Announcements', cols: ['ID', 'Created', 'Title', 'Message', 'Level', 'Expires', 'Audience'] },
  questions:{ name: 'Questions', cols: ['ID', 'Updated', 'Topic', 'Type', 'Prompt', 'Options', 'Answer', 'Explanation', 'Audience'] },
  coupons:  { name: 'Coupons',  cols: ['Code', 'Created', 'Max uses', 'Used', 'Expires', 'Note', 'Used by', 'Value per use'] },
  certs:    { name: 'Certificates', cols: ['ID', 'Issued', 'User ID', 'Name', 'Score', 'Out of', 'Exam'] },
  payments: { name: 'Payments', cols: ['ID', 'Submitted', 'User ID', 'Name', 'Mobile', 'Amount', 'Reference', 'Screenshot file ID', 'Status', 'Decided', 'Note'] },
  refunds:  { name: 'Refunds',  cols: ['ID', 'Requested', 'User ID', 'Name', 'Mobile', 'Amount', 'Send refund to', 'Reason', 'Status', 'Decided', 'Note'] },
  payouts:  { name: 'Referral payouts', cols: ['ID', 'Requested', 'User ID', 'Name', 'Mobile', 'Amount', 'Send to', 'Status', 'Decided', 'Note'] },
  visits:   { name: 'Visits', cols: ['Time', 'Source', 'Device', 'Logged in', 'Returning'] }
};
const TOPIC_KEYS = ['data', 'dist', 'prep', 'cls', 'eval', 'nn', 'ens', 'reg', 'arm', 'clu', 'py', 'math', 'nlp', 'genai', 'ops'];
const U = { id: 0, created: 1, name: 2, mobile: 3, email: 4, uni: 5, course: 6, hash: 7, salt: 8, lb: 9, prog: 10, progAt: 11, last: 12, rcode: 13, rexp: 14, consent: 15, uniEmail: 16, vuni: 17, vcode: 18, vexp: 19, exam: 20, examUsed: 21, refBy: 22, src: 23, dev: 24 };
const REF_REWARD = 1, REF_DAYS = 10;   // £1 per friend who pays and studies on 10 different days
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
    uniEmail: String(r[U.uniEmail] || ''), verifiedUni: String(r[U.vuni] || ''), examAccess: !!r[U.exam], examUsed: !!r[U.examUsed], refCode: refCode_(r[U.id]) };
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
      const ref = String(b.ref || '').toUpperCase().replace(/[^0-9A-F]/g, '');
      if (ref.length === 8) { for (let i = 1; i < all.v.length; i++) if (refCode_(all.v[i][U.id]) === ref) { while (row.length < U.refBy) row.push(''); row[U.refBy] = String(all.v[i][U.id]); break; } }
      while (row.length < U.dev + 1) row.push('');
      row[U.src] = src_(b.src) || (row[U.refBy] ? 'referral' : 'direct'); row[U.dev] = dev_(b.dev);
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
    const list = (Array.isArray(b.events) ? b.events.slice(0, 300) : []).filter(ev => ev && ['q', 'mock', 'lab'].indexOf(ev.t) >= 0);   // sales/revoke rows come only from the server
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
    const out = { ok: true, news: news, questions: questions, hidden: hidden, unis: unis_(), exam: examCfg_(), at: now };
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

  /* ----- exam access: coupon codes and manual grants ----- */
  redeem: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const key = 'redeem:' + m.uid;
    if (tooMany_(key, 8)) return fail_('too_many_attempts');
    if (m.r[U.exam]) return { ok: true, already: true, profile: profile_(m.r) };
    const code = couponCode_(b.code);
    const sh = sheet_('coupons'), v = sh.getDataRange().getValues(), i = code ? rowOf_(v, 0, code) : -1;
    if (i < 0) { bump_(key, 900); return fail_('bad_coupon'); }
    const c = v[i], max = Number(c[2]) || 1, used = Number(c[3]) || 0;
    if (c[4] && new Date(c[4]).getTime() < Date.now()) return fail_('coupon_expired');
    if (used >= max) return fail_('coupon_used_up');
    sh.getRange(i + 1, 4).setValue(used + 1);
    sh.getRange(i + 1, 7).setValue(String(c[6] || '') ? String(c[6]) + ',' + m.uid : m.uid);
    const grant = 'coupon ' + code + ' · ' + now_();
    setCell_(m.sh, m.i, U.exam, grant);
    sale_(m.uid, m.r[U.name], Number(c[7]) || 0, 'coupon ' + code);
    const row = m.r.slice(); row[U.exam] = grant;
    return { ok: true, profile: profile_(row) };
  }),
  adminCoupons: b => admin_(b, () => ({ ok: true, exam: examCfg_(), coupons: couponsList_() })),
  adminCreateCoupon: b => admin_(b, () => {
    let code = couponCode_(b.code);
    if (b.code && !code) return fail_('bad_code');
    const sh = sheet_('coupons'), v = sh.getDataRange().getValues();
    if (!code) { do { code = 'IDTA-' + sha_(Utilities.getUuid()).slice(0, 6).toUpperCase(); } while (rowOf_(v, 0, code) > 0); }
    else if (rowOf_(v, 0, code) > 0) return fail_('code_exists');
    const uses = Math.max(1, Math.min(1000, Math.floor(Number(b.uses) || 1)));
    const days = Math.max(0, Math.min(365, Math.floor(Number(b.days) || 0)));
    const value = money_(b.value);
    sh.appendRow([code, now_(), uses, 0, days ? new Date(Date.now() + days * 864e5).toISOString() : '', text_(b.note, 80), '', value]);
    return { ok: true, code: code, coupons: couponsList_() };
  }),
  adminDeleteCoupon: b => admin_(b, () => {
    const code = couponCode_(b.code);
    dropRows_(sheet_('coupons'), r => String(r[0]) !== code);
    return { ok: true, coupons: couponsList_() };
  }),
  // Grant (on = true) or remove exam access for one student, e.g. after they pay you directly.
  adminSetExam: b => admin_(b, () => {
    const all = usersAll_(), i = rowOf_(all.v, U.id, String(b.id || ''));
    if (i < 0) return fail_('no_user');
    const val = b.on ? 'granted by admin · ' + now_() : '';
    setCell_(all.sh, i, U.exam, val);
    if (b.on) sale_(String(all.v[i][U.id]), all.v[i][U.name], money_(b.amount), money_(b.amount) ? 'admin (paid)' : 'admin (free)');
    else if (all.v[i][U.exam]) sheet_('events').appendRow([now_(), String(all.v[i][U.id]), text_(all.v[i][U.name], 40), 'revoke', '', '', '', 0, '', 'admin']);
    return { ok: true, id: String(all.v[i][U.id]), examAccess: val };
  }),
  // lock: whether the course exam needs access; price: shown on the lock screen; how: how to pay (shown only to logged-in students).
  adminSetExamConfig: b => admin_(b, () => {
    const cfg = { lock: !!b.lock, price: plain_(b.price, 20) || '£3', how: String(b.how || '').split(/\r?\n/).map(l => plain_(l, 200)).join('\n').trim().slice(0, 600) };
    props_().setProperty('EXAM_CFG', JSON.stringify(cfg));
    cache_().remove('config');
    return { ok: true, exam: examCfg_() };
  }),
  adminSales: b => admin_(b, () => {
    const ev = sheet_('events').getDataRange().getValues().slice(1).filter(r => ['unlock', 'revoke', 'refund', 'payout', 'restore'].indexOf(r[3]) >= 0);
    const users = usersAll_().v.slice(1);
    return { ok: true, exam: examCfg_(), accounts: users.length, unlocked: users.filter(r => r[U.exam]).length,
      sales: ev.map(r => ({ ts: new Date(r[0]).getTime(), id: String(r[1]), name: String(r[2]).replace(/^'/, ''), kind: String(r[3]), amount: Number(r[7]) || 0, how: String(r[9] || '') })) };
  }),

  /* ----- certificates ----- */
  certIssue: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const n = Math.floor(Number(b.n)), score = Math.floor(Number(b.score)), exam = text_(b.exam, 40);
    if (n !== 30 || !(score >= 21 && score <= 30) || exam.length < 3) return fail_('not_eligible');
    const key = 'cert:' + m.uid;
    if (tooMany_(key, 10)) return fail_('too_many_attempts');
    const sh = sheet_('certs'), v = sh.getDataRange().getValues();
    for (let i = 1; i < v.length; i++) if (String(v[i][2]) === m.uid && String(v[i][6]) === exam && Number(v[i][4]) >= score) return { ok: true, cert: certOut_(v[i]) };
    bump_(key, 86400);
    let id; do { id = 'C' + sha_(Utilities.getUuid()).slice(0, 9).toUpperCase(); } while (rowOf_(v, 0, id) > 0);
    const row = [id, now_(), m.uid, text_(m.r[U.name], 40), score, n, exam];
    sh.appendRow(row);
    return { ok: true, cert: certOut_(row) };
  }),
  cert: b => {
    const id = String(b.id || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (id.length !== 10) return fail_('no_cert');
    const v = sheet_('certs').getDataRange().getValues(), i = rowOf_(v, 0, id);
    return i < 0 ? fail_('no_cert') : { ok: true, cert: certOut_(v[i]) };
  },
  myCerts: b => {
    const m = me_(b); if (!m) return fail_('auth');
    return { ok: true, certs: sheet_('certs').getDataRange().getValues().slice(1).filter(r => String(r[2]) === m.uid).map(certOut_).reverse() };
  },
  /* ----- payment screenshots: student uploads proof, admin approves ----- */
  paySubmit: b => {
    const m = me_(b); if (!m) return fail_('auth');
    if (m.r[U.exam]) return fail_('already_unlocked');
    const mt = String(b.mime || ''), data = String(b.image || '');
    if (!/^image\/(jpeg|png|webp)$/.test(mt) || data.length < 200 || data.length > 2800000 || !/^[A-Za-z0-9+/=]+$/.test(data)) return fail_('bad_image');
    return withLock_(() => {
      const key = 'pay:' + m.uid;
      if (tooMany_(key, 6)) return fail_('too_many_attempts');
      const sh = sheet_('payments'), v = sh.getDataRange().getValues();
      for (let i = 1; i < v.length; i++) if (String(v[i][2]) === m.uid && v[i][8] === 'pending') return fail_('pay_pending');
      bump_(key, 86400);
      const id = 'P' + sha_(Utilities.getUuid()).slice(0, 8).toUpperCase();
      const file = proofFolder_().createFile(Utilities.newBlob(Utilities.base64Decode(data), mt, id + '-' + String(m.r[U.name]).replace(/[^A-Za-z0-9]+/g, '_').slice(0, 30) + (mt === 'image/png' ? '.png' : mt === 'image/webp' ? '.webp' : '.jpg')));
      const amount = money_(String(examCfg_().price).replace(/[^0-9.]/g, '')) || 0;
      sh.appendRow([id, now_(), m.uid, text_(m.r[U.name], 40), "'+" + String(m.r[U.mobile]), amount, text_(b.ref, 60), file.getId(), 'pending', '', '']);
      try {
        const to = props_().getProperty('NOTIFY_EMAIL') || Session.getEffectiveUser().getEmail();
        if (to) MailApp.sendEmail({ to: to, subject: APP + ': payment to check from ' + m.r[U.name],
          body: m.r[U.name] + ' (+' + m.r[U.mobile] + ') uploaded a payment screenshot' + (b.ref ? ' (reference: ' + plain_(b.ref, 60) + ')' : '') + '.\n\nOpen your app, go to Admin -> Exam & coupons, and approve or reject it.' });
      } catch (e) {}
      return { ok: true, payment: { id: id, status: 'pending', submitted: now_() } };
    });
  },
  payStatus: b => {
    const m = me_(b); if (!m) return fail_('auth');
    const v = sheet_('payments').getDataRange().getValues();
    let last = null;
    for (let i = 1; i < v.length; i++) if (String(v[i][2]) === m.uid) last = v[i];
    return { ok: true, profile: profile_(m.r), payment: last ? { id: String(last[0]), submitted: String(last[1]), status: String(last[8]), note: String(last[10] || '').replace(/^'/, '') } : null };
  },
  adminPayments: b => admin_(b, () => {
    const v = sheet_('payments').getDataRange().getValues().slice(1).filter(r => r[0]);
    const list = v.map(r => ({ id: String(r[0]), submitted: String(r[1]), uid: String(r[2]), name: String(r[3]).replace(/^'/, ''), mobile: String(r[4]).replace(/^'/, ''), amount: Number(r[5]) || 0,
      ref: String(r[6] || '').replace(/^'/, ''), status: String(r[8]), decided: String(r[9] || ''), note: String(r[10] || '').replace(/^'/, '') })).reverse();
    return { ok: true, pending: list.filter(p => p.status === 'pending'), recent: list.filter(p => p.status !== 'pending').slice(0, 30) };
  }),
  adminPaymentImage: b => admin_(b, () => {
    const v = sheet_('payments').getDataRange().getValues(), i = rowOf_(v, 0, String(b.id || ''));
    if (i < 0) return fail_('no_payment');
    try { const blob = DriveApp.getFileById(String(v[i][7])).getBlob(); return { ok: true, mime: blob.getContentType(), image: Utilities.base64Encode(blob.getBytes()) }; }
    catch (e) { return fail_('no_image'); }
  }),
  adminDecidePayment: b => admin_(b, () => {
    const sh = sheet_('payments'), v = sh.getDataRange().getValues(), i = rowOf_(v, 0, String(b.id || ''));
    if (i < 0) return fail_('no_payment');
    if (v[i][8] !== 'pending') return fail_('already_decided');
    const ok = !!b.approve, note = text_(b.note, 120), amount = b.amount != null ? money_(b.amount) : Number(v[i][5]) || 0;
    sh.getRange(i + 1, 9, 1, 3).setValues([[ok ? 'approved' : 'rejected', now_(), note]]);
    const all = usersAll_(), u = rowOf_(all.v, U.id, String(v[i][2]));
    if (u > 0) {
      const r = all.v[u];
      if (ok && !r[U.exam]) { setCell_(all.sh, u, U.exam, 'payment ' + v[i][0] + ' · ' + now_()); sale_(String(r[U.id]), r[U.name], amount, 'payment ' + v[i][0] + (v[i][6] ? ' · ' + String(v[i][6]).replace(/^'/, '') : '')); }
      try {
        MailApp.sendEmail({ to: String(r[U.email]), subject: APP + (ok ? ': your course exams are unlocked' : ': we could not confirm your payment'),
          body: 'Hi ' + r[U.name] + ',\n\n' + (ok ? 'Your payment is confirmed and the course exams are unlocked. Open the app and go to the Mock exam tab. Good luck!' : 'We could not confirm your payment' + (note ? ': ' + plain_(note, 120) : '') + '.\n\nOpen the Mock exam tab to upload a clearer screenshot, or contact the organiser.') });
      } catch (e) {}
    }
    return { ok: true };
  }),

  /* ----- refunds: allowed only before the first course exam is started ----- */
  examStarted: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    if (m.r[U.exam] && !m.r[U.examUsed]) setCell_(m.sh, m.i, U.examUsed, now_());
    return { ok: true };
  }),
  myPurchase: b => {
    const m = me_(b); if (!m) return fail_('auth');
    const mon = money_by_user_()[m.uid] || {};
    const rf = sheet_('refunds').getDataRange().getValues().slice(1).filter(r => String(r[2]) === m.uid).pop();
    return { ok: true, access: !!m.r[U.exam], how: String(m.r[U.exam] || ''), paid: mon.paid || 0, examUsed: !!m.r[U.examUsed],
      refundable: !!m.r[U.exam] && !m.r[U.examUsed] && (mon.paid || 0) > 0 && !mon.refunded,
      refund: rf ? { id: String(rf[0]), requested: String(rf[1]), amount: Number(rf[5]) || 0, status: String(rf[8]), note: String(rf[10] || '').replace(/^'/, '') } : null };
  },
  refundRequest: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const mon = money_by_user_()[m.uid] || {};
    if (!m.r[U.exam] || !(mon.paid > 0) || mon.refunded) return fail_('not_refundable');
    if (m.r[U.examUsed]) return fail_('exam_used');
    const to = plain_(b.to, 100);
    if (to.length < 5) return fail_('bad_refund_to');
    const sh = sheet_('refunds');
    if (sh.getDataRange().getValues().slice(1).some(r => String(r[2]) === m.uid && r[8] === 'pending')) return fail_('refund_pending');
    const id = 'R' + sha_(Utilities.getUuid()).slice(0, 8).toUpperCase();
    sh.appendRow([id, now_(), m.uid, text_(m.r[U.name], 40), "'+" + String(m.r[U.mobile]), mon.paid, text_(to, 100), text_(b.reason, 200), 'pending', '', '']);
    setCell_(m.sh, m.i, U.exam, '');
    sheet_('events').appendRow([now_(), m.uid, text_(m.r[U.name], 40), 'revoke', '', '', '', 0, '', 'refund requested ' + id]);
    notifyAdmin_('refund request from ' + m.r[U.name], m.r[U.name] + ' (+' + m.r[U.mobile] + ') cancelled and asked for a refund of £' + mon.paid + '.\nSend it to: ' + to + (b.reason ? '\nReason: ' + plain_(b.reason, 200) : '') + '\n\nTheir exam access is already removed. After you send the money, open Admin -> Exam & coupons and click "Mark refunded".');
    const row = m.r.slice(); row[U.exam] = '';
    return { ok: true, profile: profile_(row) };
  }),
  adminRefunds: b => admin_(b, () => {
    const list = sheet_('refunds').getDataRange().getValues().slice(1).filter(r => r[0]).map(r => ({ id: String(r[0]), requested: String(r[1]), name: String(r[3]).replace(/^'/, ''), mobile: String(r[4]).replace(/^'/, ''),
      amount: Number(r[5]) || 0, to: String(r[6]).replace(/^'/, ''), reason: String(r[7] || '').replace(/^'/, ''), status: String(r[8]), decided: String(r[9] || ''), note: String(r[10] || '').replace(/^'/, '') })).reverse();
    return { ok: true, pending: list.filter(x => x.status === 'pending'), recent: list.filter(x => x.status !== 'pending').slice(0, 30) };
  }),
  // refunded = true: you sent the money back. false: decline and give their access back.
  adminDecideRefund: b => admin_(b, () => {
    const sh = sheet_('refunds'), v = sh.getDataRange().getValues(), i = rowOf_(v, 0, String(b.id || ''));
    if (i < 0) return fail_('no_refund');
    if (v[i][8] !== 'pending') return fail_('already_decided');
    const ok = !!b.refunded, note = text_(b.note, 120), uid = String(v[i][2]);
    sh.getRange(i + 1, 9, 1, 3).setValues([[ok ? 'refunded' : 'declined', now_(), note]]);
    const all = usersAll_(), u = rowOf_(all.v, U.id, uid);
    if (ok) sheet_('events').appendRow([now_(), uid, text_(v[i][3], 40), 'refund', '', '', '', Number(v[i][5]) || 0, '', 'refund ' + v[i][0]]);
    else if (u > 0) { setCell_(all.sh, u, U.exam, 'restored after declined refund · ' + now_()); sheet_('events').appendRow([now_(), uid, text_(v[i][3], 40), 'restore', '', '', '', 0, '', 'refund declined ' + v[i][0]]); }
    if (u > 0) try { MailApp.sendEmail({ to: String(all.v[u][U.email]), subject: APP + (ok ? ': your refund has been sent' : ': about your refund request'),
      body: 'Hi ' + all.v[u][U.name] + ',\n\n' + (ok ? 'Your refund of £' + v[i][5] + ' has been sent to ' + String(v[i][6]).replace(/^'/, '') + '.' : 'Your refund request was declined' + (note ? ': ' + plain_(note, 120) : '') + '. Your exam access has been restored.') }); } catch (e) {}
    return { ok: true };
  }),

  /* ----- referrals: £1 when a friend you invited pays and studies on 10 different days ----- */
  myReferrals: b => {
    const m = me_(b); if (!m) return fail_('auth');
    const r = referralsOf_(m.uid);
    return Object.assign({ ok: true, code: refCode_(m.uid), reward: REF_REWARD, days: REF_DAYS }, r);
  },
  referralPayout: b => withLock_(() => {
    const m = me_(b); if (!m) return fail_('auth');
    const r = referralsOf_(m.uid), to = plain_(b.to, 100);
    if (r.available < REF_REWARD) return fail_('nothing_to_pay');
    if (to.length < 5) return fail_('bad_refund_to');
    const id = 'X' + sha_(Utilities.getUuid()).slice(0, 8).toUpperCase();
    sheet_('payouts').appendRow([id, now_(), m.uid, text_(m.r[U.name], 40), "'+" + String(m.r[U.mobile]), r.available, text_(to, 100), 'pending', '', '']);
    notifyAdmin_('referral payout request from ' + m.r[U.name], m.r[U.name] + ' (+' + m.r[U.mobile] + ') asked for their referral reward of £' + r.available + '.\nSend it to: ' + to + '\n\nAfter you pay, open Admin -> Exam & coupons and click "Mark paid".');
    return Object.assign({ ok: true }, referralsOf_(m.uid));
  }),
  adminReferrals: b => admin_(b, () => {
    const users = usersAll_().v.slice(1), ids = {};
    users.forEach(r => { if (r[U.refBy]) (ids[String(r[U.refBy])] = ids[String(r[U.refBy])] || []).push(r); });
    const name = {}; users.forEach(r => name[String(r[U.id])] = String(r[U.name]));
    const top = Object.keys(ids).map(uid => { const r = referralsOf_(uid); return { name: name[uid] || '(deleted)', invited: r.friends.length, qualified: r.qualified, earned: r.earned, paid: r.paidOut, pending: r.pending }; }).sort((a, b) => b.qualified - a.qualified || b.invited - a.invited);
    const list = sheet_('payouts').getDataRange().getValues().slice(1).filter(r => r[0]).map(r => ({ id: String(r[0]), requested: String(r[1]), name: String(r[3]).replace(/^'/, ''), mobile: String(r[4]).replace(/^'/, ''),
      amount: Number(r[5]) || 0, to: String(r[6]).replace(/^'/, ''), status: String(r[7]), decided: String(r[8] || ''), note: String(r[9] || '').replace(/^'/, '') })).reverse();
    return { ok: true, reward: REF_REWARD, days: REF_DAYS, referrers: top, pending: list.filter(x => x.status === 'pending'), recent: list.filter(x => x.status !== 'pending').slice(0, 30) };
  }),
  adminDecidePayout: b => admin_(b, () => {
    const sh = sheet_('payouts'), v = sh.getDataRange().getValues(), i = rowOf_(v, 0, String(b.id || ''));
    if (i < 0) return fail_('no_payout');
    if (v[i][7] !== 'pending') return fail_('already_decided');
    const ok = !!b.paid, note = text_(b.note, 120);
    sh.getRange(i + 1, 8, 1, 3).setValues([[ok ? 'paid' : 'declined', now_(), note]]);
    if (ok) sheet_('events').appendRow([now_(), String(v[i][2]), text_(v[i][3], 40), 'payout', '', '', '', Number(v[i][5]) || 0, '', 'referral payout ' + v[i][0]]);
    return { ok: true };
  }),

  /* ----- analytics: first-party counts only, no cookies or third-party trackers ----- */
  hit: b => {
    if (tooMany_('hits', 20000)) return { ok: true };   // flood guard
    bump_('hits', 86400);
    const sh = sheet_('visits');
    sh.appendRow([now_(), src_(b.src) || 'direct', dev_(b.dev), b.li ? 'yes' : 'no', b.ret ? 'yes' : 'no']);
    return { ok: true };
  },
  adminAnalytics: b => admin_(b, () => {
    const days = [7, 30, 90, 365].indexOf(Number(b.days)) >= 0 ? Number(b.days) : 30;
    const now = Date.now(), from = now - days * 864e5, dayK = t => new Date(t).toISOString().slice(0, 10);
    const keys = []; for (let i = Math.min(days, 90) - 1; i >= 0; i--) keys.push(dayK(now - i * 864e5));
    const series = () => { const o = {}; keys.forEach(k => o[k] = 0); return o; };
    const visits = sheet_('visits').getDataRange().getValues().slice(1).filter(r => r[0]).map(r => ({ t: new Date(r[0]).getTime(), src: String(r[1] || 'direct'), dev: String(r[2] || ''), li: r[3] === 'yes' }));
    const users = usersAll_().v.slice(1).filter(r => r[U.id]).map(r => ({ id: String(r[U.id]), t: new Date(r[U.created]).getTime(), src: String(r[U.src] || 'unknown'), dev: String(r[U.dev] || 'unknown'), ref: !!r[U.refBy] }));
    const ev = sheet_('events').getDataRange().getValues().slice(1).filter(r => r[1]);
    const certs = sheet_('certs').getDataRange().getValues().slice(1).filter(r => r[0]);
    const act = {}, unlockAt = {}, revenue = {}, mockers = {};
    ev.forEach(r => {
      const t = new Date(r[0]).getTime(), uid = String(r[1]), e = r[3];
      if (e === 'q' || e === 'mock' || e === 'lab') (act[uid] = act[uid] || []).push(t);
      if (e === 'mock') mockers[uid] = Math.min(mockers[uid] || Infinity, t);
      if (e === 'unlock') { if (!unlockAt[uid]) unlockAt[uid] = t; revenue[uid] = (revenue[uid] || 0) + (Number(r[7]) || 0); }
      if (e === 'refund' || e === 'payout') revenue[uid] = (revenue[uid] || 0);
    });
    // funnel for the period (sign-ups in the period and what they went on to do)
    const vIn = visits.filter(v => v.t >= from && !v.li), uIn = users.filter(u => u.t >= from), certUids = {};
    certs.forEach(r => certUids[String(r[2])] = 1);
    const funnel = { visits: vIn.length, signups: uIn.length, studied: uIn.filter(u => act[u.id]).length, unlocked: uIn.filter(u => unlockAt[u.id]).length, exams: uIn.filter(u => mockers[u.id]).length, certs: uIn.filter(u => certUids[u.id]).length };
    // daily series
    const dv = series(), ds = series(), da = series(), du = series();
    vIn.forEach(v => { const k = dayK(v.t); if (k in dv) dv[k]++; });
    uIn.forEach(u => { const k = dayK(u.t); if (k in ds) ds[k]++; });
    const seen = {}; Object.keys(act).forEach(uid => act[uid].forEach(t => { const k = dayK(t); if (k in da && !seen[uid + k]) { seen[uid + k] = 1; da[k]++; } }));
    Object.keys(unlockAt).forEach(uid => { const k = dayK(unlockAt[uid]); if (k in du) du[k]++; });
    // sources
    const src = {};
    const S = k => src[k] = src[k] || { visits: 0, signups: 0, unlocked: 0, revenue: 0 };
    vIn.forEach(v => S(v.src).visits++);
    uIn.forEach(u => { const o = S(u.src); o.signups++; if (unlockAt[u.id]) o.unlocked++; o.revenue += revenue[u.id] || 0; });
    // engagement
    const activeSince = since => Object.keys(act).filter(uid => act[uid].some(t => t >= since)).length;
    const minutes = uid => { const ts = act[uid].filter(t => t >= from).sort((a, b) => a - b); let m = 0; for (let i = 0; i < ts.length; i++) m += i && ts[i] - ts[i - 1] < 30 * 6e4 ? Math.min(5, (ts[i] - ts[i - 1]) / 6e4) : 1; return m; };
    const activeIn = Object.keys(act).filter(uid => act[uid].some(t => t >= from));
    const daysOf = uid => Object.keys(act[uid].filter(t => t >= from).reduce((o, t) => (o[dayK(t)] = 1, o), {})).length;
    const engagement = { dau: activeSince(now - 864e5), wau: activeSince(now - 7 * 864e5), mau: activeSince(now - 30 * 864e5), active: activeIn.length,
      avgDays: activeIn.length ? Math.round(activeIn.reduce((s, u) => s + daysOf(u), 0) / activeIn.length * 10) / 10 : 0,
      avgMinutes: activeIn.length ? Math.round(activeIn.reduce((s, u) => s + minutes(u), 0) / activeIn.length) : 0 };
    const devices = { visits: {}, signups: {} };
    vIn.forEach(v => devices.visits[v.dev || 'unknown'] = (devices.visits[v.dev || 'unknown'] || 0) + 1);
    uIn.forEach(u => devices.signups[u.dev] = (devices.signups[u.dev] || 0) + 1);
    // retention: share of students who came back N or more days after signing up (only students old enough to count)
    const ret = n => { const pool = users.filter(u => u.t <= now - n * 864e5); const back = pool.filter(u => (act[u.id] || []).some(t => t >= u.t + n * 864e5)); return { pool: pool.length, back: back.length }; };
    return { ok: true, days: days, keys: keys, funnel: funnel, daily: { visits: keys.map(k => dv[k]), signups: keys.map(k => ds[k]), active: keys.map(k => da[k]), unlocks: keys.map(k => du[k]) },
      sources: Object.keys(src).map(k => Object.assign({ src: k }, src[k])).sort((a, b) => b.signups - a.signups || b.visits - a.visits), engagement: engagement, devices: devices,
      retention: { d1: ret(1), d7: ret(7), d30: ret(30) }, totals: { users: users.length, visits: visits.length } };
  }),
  adminStudent: b => admin_(b, () => {
    const all = usersAll_(), i = rowOf_(all.v, U.id, String(b.id || ''));
    if (i < 0) return fail_('no_user');
    const r = all.v[i], uid = String(r[U.id]), prog = progress_(r) || {};
    const ev = sheet_('events').getDataRange().getValues().slice(1).filter(x => String(x[1]) === uid)
      .map(x => ({ ts: new Date(x[0]).getTime(), ev: String(x[3]), topic: String(x[4] || ''), qid: x[5], ok: x[6], s: x[7], n: x[8], k: String(x[9] || '') }));
    const ref = r[U.refBy] ? all.v[rowOf_(all.v, U.id, String(r[U.refBy]))] : null;
    const pick = (sheetKey, f) => sheet_(sheetKey).getDataRange().getValues().slice(1).filter(x => String(x[2]) === uid).map(f);
    return { ok: true,
      profile: { id: uid, name: String(r[U.name]), mobile: String(r[U.mobile]), email: String(r[U.email]), university: String(r[U.uni]), course: String(r[U.course]), created: String(r[U.created]), lastLogin: String(r[U.last]),
        verifiedUni: String(r[U.vuni] || ''), exam: String(r[U.exam] || ''), examUsed: String(r[U.examUsed] || ''), src: String(r[U.src] || ''), dev: String(r[U.dev] || ''), referredBy: ref ? String(ref[U.name]) : '' },
      lessons: (prog['idta-lessons'] || []).length, mustTicked: (prog['idta-must'] || []).length, mistakes: (prog['idta-miss'] || []).length, goal: prog['idta-goal'] || null, examDate: prog['idta-examdate'] || null,
      events: ev.slice(-2000),
      certs: pick('certs', x => ({ id: String(x[0]), issued: String(x[1]), score: Number(x[4]), n: Number(x[5]), exam: String(x[6]) })),
      payments: pick('payments', x => ({ id: String(x[0]), at: String(x[1]), amount: Number(x[5]) || 0, status: String(x[8]) })),
      refunds: pick('refunds', x => ({ id: String(x[0]), at: String(x[1]), amount: Number(x[5]) || 0, status: String(x[8]) })),
      referrals: referralsOf_(uid) };
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
        uniEmail: String(r[U.uniEmail] || ''), verifiedUni: String(r[U.vuni] || ''), examAccess: String(r[U.exam] || '')
      }, s.users[String(r[U.id])] || blank_()))
    };
  }
};
function examCfg_() {
  let c = null;
  try { c = JSON.parse(props_().getProperty('EXAM_CFG') || 'null'); } catch (e) {}
  c = c || {};
  return { lock: c.lock !== false, price: c.price || '£3', how: c.how || '', refReward: REF_REWARD, refDays: REF_DAYS };
}
function money_(v) { const n = Math.round(Number(v) * 100) / 100; return isFinite(n) && n > 0 ? Math.min(n, 1000) : 0; }
function sale_(uid, name, amount, how) { sheet_('events').appendRow([now_(), uid, text_(name, 40), 'unlock', '', '', '', amount, '', text_(how, 60)]); }
function certOut_(r) { return { id: String(r[0]), issued: String(r[1]), name: String(r[3]).replace(/^'/, ''), score: Number(r[4]), n: Number(r[5]), exam: String(r[6]).replace(/^'/, '') }; }
function src_(v) { return String(v || '').toLowerCase().replace(/[^a-z0-9._-]/g, '').slice(0, 40); }
function dev_(v) { return ['phone', 'tablet', 'desktop'].indexOf(String(v)) >= 0 ? String(v) : 'unknown'; }
function refCode_(uid) { return pid_(uid).slice(0, 8).toUpperCase(); }
function notifyAdmin_(subject, body) {
  try { const to = props_().getProperty('NOTIFY_EMAIL') || Session.getEffectiveUser().getEmail(); if (to) MailApp.sendEmail({ to: to, subject: APP + ': ' + subject, body: body }); } catch (e) {}
}
// Per student: amount paid for the latest unlock, whether it was refunded, and the set of days they studied.
function money_by_user_() {
  const out = {};
  sheet_('events').getDataRange().getValues().slice(1).forEach(r => {
    const uid = String(r[1]), ev = r[3]; if (!uid) return;
    const o = out[uid] = out[uid] || { paid: 0, refunded: false, days: {} };
    if (ev === 'unlock') { o.paid = Number(r[7]) || 0; o.refunded = false; }
    else if (ev === 'refund') o.refunded = true;
    else if (ev === 'q' || ev === 'mock' || ev === 'lab') { const t = new Date(r[0]); if (!isNaN(t)) o.days[t.toISOString().slice(0, 10)] = 1; }
  });
  return out;
}
function referralsOf_(uid) {
  const users = usersAll_().v.slice(1), mon = money_by_user_();
  const friends = users.filter(r => String(r[U.refBy]) === uid).map(r => {
    const o = mon[String(r[U.id])] || { paid: 0, refunded: false, days: {} }, paid = o.paid > 0 && !o.refunded && !!r[U.exam], days = Object.keys(o.days).length;
    return { name: String(r[U.name]).split(' ')[0], joined: String(r[U.created]), paid: paid, days: Math.min(days, REF_DAYS), qualified: paid && days >= REF_DAYS };
  });
  const qualified = friends.filter(f => f.qualified).length, earned = qualified * REF_REWARD;
  let paidOut = 0, pending = 0;
  sheet_('payouts').getDataRange().getValues().slice(1).forEach(r => { if (String(r[2]) !== uid) return; if (r[7] === 'paid') paidOut += Number(r[5]) || 0; else if (r[7] === 'pending') pending += Number(r[5]) || 0; });
  return { friends: friends, qualified: qualified, earned: earned, paidOut: paidOut, pending: pending, available: Math.max(0, earned - paidOut - pending) };
}
function proofFolder_() {
  const p = props_(), id = p.getProperty('PROOF_FOLDER');
  if (id) { try { return DriveApp.getFolderById(id); } catch (e) {} }
  const f = DriveApp.createFolder('IDTA Revision payment screenshots (private)');
  p.setProperty('PROOF_FOLDER', f.getId());
  return f;
}
function couponCode_(v) { const s = String(v || '').toUpperCase().replace(/[^A-Z0-9-]/g, ''); return s.length >= 4 && s.length <= 24 ? s : ''; }
function couponsList_() {
  return sheet_('coupons').getDataRange().getValues().slice(1).filter(r => r[0]).map(r => ({
    code: String(r[0]), created: String(r[1]), max: Number(r[2]) || 1, used: Number(r[3]) || 0, expires: String(r[4] || ''), note: String(r[5] || '').replace(/^'/, ''),
    usedBy: String(r[6] || '').split(',').filter(Boolean).length, value: Number(r[7]) || 0 })).reverse();
}
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
  try { return out_(p.action === 'leaderboard' ? API.leaderboard() : p.action === 'config' ? API.config() : p.action === 'cert' ? API.cert(p) : API.ping()); }
  catch (err) { return out_(fail_('server_error')); }
}

/* Run once from the editor: creates the sheets, the pepper, and asks for Gmail permission (for reset codes). */
function setup() {
  ['users', 'sessions', 'events', 'news', 'questions', 'coupons', 'certs', 'payments', 'refunds', 'payouts', 'visits'].forEach(k => { const sh = sheet_(k); sh.getRange(1, 1, 1, SHEETS[k].cols.length).setValues([SHEETS[k].cols]).setFontWeight('bold'); });
  if (!props_().getProperty('UNI_DOMAINS')) props_().setProperty('UNI_DOMAINS', JSON.stringify(DEFAULT_DOMAINS));
  Logger.log('University packs installed: ' + (Object.keys(packs_()).join(', ') || 'none (add Pack.gs)'));
  pepper_();
  Logger.log('Data sheet ready: ' + book_().getUrl());
  Logger.log('Emails left today (for reset codes): ' + MailApp.getRemainingDailyQuota());
  Logger.log('Payment screenshots folder: ' + proofFolder_().getUrl());
  Logger.log(props_().getProperty('ADMIN_KEY') ? 'ADMIN_KEY is set.' : 'ADMIN_KEY is NOT set: run setAdminKey first.');
}

/* Removes expired sessions. Optional: add a daily time trigger for this. */
function cleanSessions() {
  withLock_(() => dropRows_(sheet_('sessions'), r => new Date(r[3]).getTime() > Date.now()));
}
