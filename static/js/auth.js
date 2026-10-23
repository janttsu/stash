// Sign-in / create-account screen, shared by the app and the bookmarklet popup.
import { api, fill, h } from './core.js';
import { getLang, t } from './i18n.js';

// the reset link from the e-mail carries its token after a # so that it never reaches the server's logs
const resetTokenFromLink = () => (location.hash.match(/^#reset=([\w-]+)$/) || [])[1] || '';

export async function renderAuth(container, onDone) {
  const invite = new URLSearchParams(location.search).get('invite') || '';
  const resetToken = resetTokenFromLink();
  let config = { registration: 'closed', first_user: false };
  try {
    config = await api('GET', '/api/public');
  } catch { /* the sign-in form still works */ }
  const canRegister = config.registration === 'open' || (config.registration === 'invite' && Boolean(invite));
  let mode = resetToken ? 'reset' : (canRegister && (config.first_user || invite) ? 'register' : 'login');
  let notice = '';   // a message above the form: after a reset link was sent, or after a password was changed

  // a reset link opened in a tab that already shows this page only changes the # part of the address
  window.addEventListener('hashchange', () => { if (resetTokenFromLink()) renderAuth(container, onDone); }, { once: true });

  const draw = () => {
    const username = h('input', {
      type: 'text', class: 'input', required: true, maxLength: 32, autocomplete: 'username', autocapitalize: 'none',
      spellcheck: false, autofocus: true,
    });
    const email = h('input', {
      type: 'email', class: 'input', required: mode === 'forgot', maxLength: 254, autocomplete: 'email', autocapitalize: 'none',
      spellcheck: false, autofocus: mode === 'forgot',
    });
    const password = h('input', {
      type: 'password', class: 'input', required: true, maxLength: 200,
      minLength: mode === 'register' || mode === 'reset' ? 8 : 1,
      autocomplete: mode === 'register' || mode === 'reset' ? 'new-password' : 'current-password',
    });
    const again = h('input', { type: 'password', class: 'input', required: true, maxLength: 200, autocomplete: 'new-password' });
    const totp = h('input', {
      type: 'text', class: 'input', inputMode: 'numeric', autocomplete: 'one-time-code', maxLength: 10, placeholder: '123456',
    });
    const remember = h('input', { type: 'checkbox' });
    const rememberField = h('label', { class: 'check' }, remember, h('span', null, t('Remember this browser for 30 days')));
    const totpField = h('label', { class: 'field', hidden: true }, h('span', null, t('Code from your authenticator app')), totp);
    const error = h('p', { class: 'form-error', hidden: true });
    const info = h('p', { class: 'muted center', hidden: !notice }, notice);
    const headings = {
      login: t('Sign in'), register: t('Create account'), forgot: t('Send reset link'), reset: t('Save new password'),
    };
    const submit = h('button', { type: 'submit', class: 'btn primary block' }, headings[mode]);

    const switchTo = (next) => { mode = next; error.hidden = true; draw(); };

    const onsubmit = async (e) => {
      e.preventDefault();
      error.hidden = true;
      submit.disabled = true;
      try {
        if (mode === 'register') {
          if (password.value !== again.value) throw new Error(t('The passwords do not match.'));
          await api('POST', '/api/register', {
            username: username.value, password: password.value, email: email.value, invite, lang: getLang(),
          });
        } else if (mode === 'forgot') {
          await api('POST', '/api/password/forgot', { email: email.value, lang: getLang() });
          notice = t('If an account has this address, a link to choose a new password is on its way. The link works for an hour.');
          switchTo('login');
          return;
        } else if (mode === 'reset') {
          if (password.value !== again.value) throw new Error(t('The passwords do not match.'));
          await api('POST', '/api/password/reset', { token: resetToken, password: password.value });
          history.replaceState(null, '', location.pathname + location.search);
          notice = t('Your password is changed. Sign in with the new one.');
          switchTo('login');
          return;
        } else {
          await api('POST', '/api/login', {
            username: username.value, password: password.value, totp: totp.value, remember: remember.checked,
          });
        }
        onDone();
      } catch (err) {
        if (err.code === 'totp_required' || err.code === 'bad_totp') {
          totpField.hidden = false;
          totp.focus();
        }
        error.textContent = err.message;
        error.hidden = false;
      } finally {
        submit.disabled = false;
      }
    };

    const form = mode === 'forgot' ? [
      h('label', { class: 'field' }, h('span', null, t('E-mail address')), email),
    ] : mode === 'reset' ? [
      h('label', { class: 'field' }, h('span', null, t('New password (at least 8 characters)')), password),
      h('label', { class: 'field' }, h('span', null, t('Password again')), again),
    ] : [
      h('label', { class: 'field' }, h('span', null, t('Username')), username),
      mode === 'register' && h('label', { class: 'field' }, h('span', null, t('E-mail address (optional, for resetting the password)')), email),
      h('label', { class: 'field' }, h('span', null, mode === 'register' ? t('Password (at least 8 characters)') : t('Password')), password),
      mode === 'register' && h('label', { class: 'field' }, h('span', null, t('Password again')), again),
      mode === 'login' && totpField,
      mode === 'login' && rememberField,
    ];

    fill(container, h('div', { class: 'auth' },
      h('div', { class: 'auth__brand' }, h('img', { src: '/static/icon.svg', alt: '', width: 48, height: 48 }), h('h1', null, 'Stash')),
      h('p', { class: 'muted center' }, mode === 'reset' ? t('Choose a new password for your account.')
        : mode === 'forgot' ? t('Enter the e-mail address of your account. We send a link to choose a new password.')
          : config.first_user
            ? t('Welcome! Create the first account – it becomes the administrator.')
            : t('Your bookmarks, organised and private.')),
      info,
      h('form', { class: 'form card', onsubmit }, ...form, error, submit),
      mode === 'login' && h('p', { class: 'center' }, h('button', {
        type: 'button', class: 'linklike', onclick: () => switchTo('forgot'),
      }, t('Forgot your password?'))),
      (mode === 'forgot' || mode === 'reset') && h('p', { class: 'center' }, h('button', {
        type: 'button', class: 'linklike', onclick: () => switchTo('login'),
      }, t('Back to sign in'))),
      mode !== 'reset' && canRegister && !config.first_user && h('p', { class: 'center' }, h('button', {
        type: 'button', class: 'linklike',
        onclick: () => switchTo(mode === 'login' ? 'register' : 'login'),
      }, mode === 'login' ? t('Create an account') : t('I already have an account'))),
      mode === 'login' && !canRegister && config.registration === 'invite' && h('p', { class: 'muted center small' }, config.first_user
        ? t('No account exists yet. Open the invitation link printed by the server command “python -m app.cli invite”.')
        : t('New accounts are by invitation only.'))));
    (mode === 'forgot' ? email : mode === 'reset' ? password : username).focus();
  };
  draw();
}
