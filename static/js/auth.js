// Sign-in / create-account screen, shared by the app and the bookmarklet popup.
import { api, fill, h } from './core.js';
import { getLang, t } from './i18n.js';

export async function renderAuth(container, onDone) {
  const invite = new URLSearchParams(location.search).get('invite') || '';
  let config = { registration: 'closed', first_user: false };
  try {
    config = await api('GET', '/api/public');
  } catch { /* the sign-in form still works */ }
  const canRegister = config.registration === 'open' || (config.registration === 'invite' && Boolean(invite));
  let mode = canRegister && (config.first_user || invite) ? 'register' : 'login';

  const draw = () => {
    const username = h('input', {
      type: 'text', class: 'input', required: true, maxLength: 32, autocomplete: 'username', autocapitalize: 'none',
      spellcheck: false, autofocus: true,
    });
    const password = h('input', {
      type: 'password', class: 'input', required: true, maxLength: 200,
      minLength: mode === 'register' ? 8 : 1, autocomplete: mode === 'register' ? 'new-password' : 'current-password',
    });
    const again = h('input', { type: 'password', class: 'input', required: true, maxLength: 200, autocomplete: 'new-password' });
    const totp = h('input', {
      type: 'text', class: 'input', inputMode: 'numeric', autocomplete: 'one-time-code', maxLength: 10, placeholder: '123456',
    });
    const remember = h('input', { type: 'checkbox' });
    const rememberField = h('label', { class: 'check' }, remember, h('span', null, t('Remember this browser for 30 days')));
    const totpField = h('label', { class: 'field', hidden: true }, h('span', null, t('Code from your authenticator app')), totp);
    const error = h('p', { class: 'form-error', hidden: true });
    const submit = h('button', { type: 'submit', class: 'btn primary block' },
      mode === 'register' ? t('Create account') : t('Sign in'));

    const onsubmit = async (e) => {
      e.preventDefault();
      error.hidden = true;
      submit.disabled = true;
      try {
        if (mode === 'register') {
          if (password.value !== again.value) throw new Error(t('The passwords do not match.'));
          await api('POST', '/api/register', { username: username.value, password: password.value, invite, lang: getLang() });
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

    fill(container, h('div', { class: 'auth' },
      h('div', { class: 'auth__brand' }, h('img', { src: '/static/icon.svg', alt: '', width: 48, height: 48 }), h('h1', null, 'Stash')),
      h('p', { class: 'muted center' }, config.first_user
        ? t('Welcome! Create the first account – it becomes the administrator.')
        : t('Your bookmarks, organised and private.')),
      h('form', { class: 'form card', onsubmit },
        h('label', { class: 'field' }, h('span', null, t('Username')), username),
        h('label', { class: 'field' }, h('span', null, mode === 'register' ? t('Password (at least 8 characters)') : t('Password')), password),
        mode === 'register' && h('label', { class: 'field' }, h('span', null, t('Password again')), again),
        mode === 'login' && totpField,
        mode === 'login' && rememberField,
        error,
        submit),
      canRegister && !config.first_user && h('p', { class: 'center' }, h('button', {
        type: 'button', class: 'linklike',
        onclick: () => { mode = mode === 'login' ? 'register' : 'login'; draw(); },
      }, mode === 'login' ? t('Create an account') : t('I already have an account'))),
      !canRegister && config.registration === 'invite' && h('p', { class: 'muted center small' }, config.first_user
        ? t('No account exists yet. Open the invitation link printed by the server command “python -m app.cli invite”.')
        : t('New accounts are by invitation only.'))));
    username.focus();
  };
  draw();
}
