/* Small progressive enhancements; all editing/navigation works without JavaScript. */
(() => {
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-audio]');
    if (!button) return;
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'metadata';
    audio.src = button.dataset.audio;
    document.querySelectorAll('audio').forEach((player) => player.pause());
    button.replaceWith(audio);
    audio.play().catch(() => {});
  });

  const panel = document.querySelector('[data-library]');
  if (panel) {
    let busy = false;
    const refresh = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const response = await fetch(panel.dataset.statusUrl + location.search, {headers: {'Accept': 'application/json'}});
        if (!response.ok || response.redirected) return;
        const data = await response.json();
        for (const [name, count] of Object.entries(data.counts)) {
          document.querySelectorAll(`[data-count="${name}"]`).forEach((node) => { node.textContent = count; });
        }
        for (const asset of data.assets) {
          const row = document.querySelector(`[data-asset="${asset.id}"]`);
          if (!row) continue;
          const badge = row.querySelector('[data-badge]');
          badge.className = 'badge ' + asset.status;
          badge.textContent = asset.label;
          row.querySelector('[data-title]').textContent = asset.title;
          if (asset.artist) row.querySelector('[data-artist]').textContent = asset.artist;
          row.querySelector('[data-duration]').textContent = asset.duration;
          const preview = row.querySelector('[data-preview]');
          if (asset.audio_url && !preview.childElementCount) {
            const button = document.createElement('button');
            button.className = 'preview-trigger';
            button.type = 'button';
            button.dataset.audio = asset.audio_url;
            button.textContent = '▷ Preascolta';
            button.setAttribute('aria-label', 'Preascolta ' + asset.title);
            preview.append(button);
          }
        }
      } catch (_) { /* A later poll retries without disturbing the user's work. */ }
      finally { busy = false; }
    };
    setInterval(refresh, 4000);
  }

  const form = document.querySelector('[data-upload]');
  if (form) {
    const input = form.querySelector('[type=file]');
    const names = form.querySelector('[data-file-list]');
    const progress = form.querySelector('[data-upload-progress]');
    const button = form.querySelector('[data-upload-button]');
    input.addEventListener('change', () => {
      names.textContent = [...input.files].map((file) => file.name).join(' · ') || 'Nessun file selezionato';
    });
    form.addEventListener('submit', (event) => {
      if (!form.checkValidity()) return;
      event.preventDefault();
      const request = new XMLHttpRequest();
      request.open('POST', form.action || location.href);
      request.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
      request.upload.addEventListener('progress', (event) => {
        progress.textContent = event.lengthComputable ? `Caricamento: ${Math.round(event.loaded / event.total * 100)}%` : 'Caricamento in corso…';
      });
      request.upload.addEventListener('load', () => { progress.textContent = 'Caricamento completato. Salvataggio dei file…'; });
      request.addEventListener('load', () => {
        try {
          const data = JSON.parse(request.responseText);
          if (request.status === 200 && data.redirect) { location.assign(data.redirect); return; }
          progress.textContent = data.error || 'Caricamento non riuscito. Controlla i file e riprova.';
        } catch (_) { progress.textContent = 'Sessione scaduta o caricamento non riuscito. Ricarica la pagina e riprova.'; }
        button.disabled = false;
      });
      request.addEventListener('error', () => { progress.textContent = 'Connessione interrotta. Puoi riprovare: i duplicati verranno riconosciuti.'; button.disabled = false; });
      button.disabled = true;
      progress.textContent = 'Caricamento in corso…';
      request.send(new FormData(form));
    });
  }
})();

// Playlist filtering only hides options; checked memberships remain selected.
const playlistSearch = document.querySelector('[data-playlist-search]');
if (playlistSearch) {
  playlistSearch.addEventListener('input', () => {
    const query = playlistSearch.value.toLocaleLowerCase('it').trim();
    document.querySelectorAll('[data-playlist-option]').forEach((row) => {
      row.hidden = !row.textContent.toLocaleLowerCase('it').includes(query);
    });
  });
}

// Hide optional calendar controls without making JavaScript a requirement for upload.
const uploadSchedule = document.querySelector('[data-upload-schedule]');
if (uploadSchedule) {
  const category = document.querySelector('#id_category');
  const enabled = document.querySelector('#id_schedule');
  const date = document.querySelector('#id_scheduled_at');
  const button = document.querySelector('[data-upload-button]');
  const refreshSchedule = () => {
    const podcast = category.value === 'podcasts';
    uploadSchedule.hidden = !podcast;
    enabled.disabled = !podcast;
    const scheduling = podcast && enabled.checked;
    document.querySelector('[data-schedule-time]').hidden = !scheduling;
    date.disabled = !scheduling;
    date.required = scheduling;
    button.textContent = scheduling ? 'Carica e programma ↗' : 'Carica e prepara ↗';
  };
  category.addEventListener('change', refreshSchedule);
  enabled.addEventListener('change', refreshSchedule);
  refreshSchedule();
}


const uploadPlaylistGroup = document.querySelector('[data-upload-playlists]');
if (uploadPlaylistGroup) {
  const category = document.querySelector('#id_category');
  const refreshPlaylists = () => {
    const music = category.value === 'music';
    uploadPlaylistGroup.hidden = !music;
    uploadPlaylistGroup.querySelectorAll('input').forEach((input) => { input.disabled = !music; });
  };
  category.addEventListener('change', refreshPlaylists);
  refreshPlaylists();
}

// Save only this song's memberships. The ordinary POST form remains a fallback.
document.querySelectorAll('[data-row-playlists]').forEach((rowForm) => {
  const menu = rowForm.querySelector('[popover]');
  const toggle = rowForm.querySelector('[popovertarget]');
  const positionMenu = () => {
    if (!menu.matches(':popover-open')) return;
    const anchor = toggle.getBoundingClientRect();
    const width = Math.min(280, window.innerWidth - 16);
    menu.style.width = `${width}px`;
    menu.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))}px`;
    const below = anchor.bottom + 4;
    menu.style.top = `${below + menu.offsetHeight <= window.innerHeight - 8 ? below : Math.max(8, anchor.top - menu.offsetHeight - 4)}px`;
  };
  menu.addEventListener('toggle', () => {
    toggle.setAttribute('aria-expanded', String(menu.matches(':popover-open')));
    positionMenu();
  });
  window.addEventListener('resize', positionMenu);
  window.addEventListener('scroll', positionMenu, true);
  const feedback = rowForm.querySelector('[data-playlist-feedback]');
  const caption = rowForm.querySelector('[data-playlist-caption]');
  const inputs = [...rowForm.querySelectorAll('input[name="playlists"]')];
  const submit = rowForm.querySelector('button[type="submit"]');
  let saving = false;
  rowForm.addEventListener('change', () => {
    feedback.textContent = 'Da confermare';
    feedback.classList.remove('error');
  });
  rowForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    const data = new FormData(rowForm);
    submit.disabled = true;
    inputs.forEach((input) => { input.disabled = true; });
    feedback.textContent = 'Salvataggio…';
    feedback.classList.remove('error');
    try {
      const response = await fetch(rowForm.action, {method: 'POST', body: data, headers: {'X-Requested-With': 'XMLHttpRequest'}});
      if (response.redirected) throw new Error('Sessione scaduta: ricarica la pagina.');
      if (!response.headers.get('Content-Type')?.includes('application/json')) {
        throw new Error('Non salvato: ricarica la pagina e riprova.');
      }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Non salvato. Ricarica la pagina e riprova.');
      const memberships = result.playlists;
      const ids = new Set(memberships.map((playlist) => String(playlist.id)));
      inputs.forEach((input) => { input.checked = ids.has(input.value); });
      caption.textContent = memberships.length ? memberships[0].name + (memberships.length > 1 ? ` +${memberships.length - 1}` : '') : 'Seleziona playlist';
      const filter = new URLSearchParams(location.search).get('playlist');
      const mismatch = filter && (filter === 'none' ? memberships.length > 0 : !ids.has(filter));
      feedback.textContent = mismatch ? 'Salvato · ricarica per aggiornare il filtro' : 'Salvato';
      menu.hidePopover();
    } catch (error) {
      feedback.textContent = error.message || 'Connessione interrotta: riprova.';
      feedback.classList.add('error');
    } finally {
      inputs.forEach((input) => { input.disabled = false; });
      submit.disabled = false;
      saving = false;
    }
  });
});

const copyPodcastLink = document.querySelector('[data-copy-link]');
if (copyPodcastLink) {
  copyPodcastLink.addEventListener('click', async () => {
    const input = document.querySelector('#public-audio-link');
    const feedback = document.querySelector('[data-copy-feedback]');
    try {
      await navigator.clipboard.writeText(input.value);
      feedback.textContent = 'Link copiato';
    } catch (_) {
      input.focus(); input.select();
      feedback.textContent = 'Link selezionato: copialo con il comando Copia del tuo dispositivo.';
    }
  });
}

const uploadReplay = document.querySelector('[data-upload-replay]');
if (uploadReplay) {
  const category = document.querySelector('#id_category');
  const checkbox = document.querySelector('#id_replay_after_broadcast');
  const refreshReplay = () => {
    const podcast = category.value === 'podcasts';
    uploadReplay.hidden = !podcast;
    checkbox.disabled = !podcast;
  };
  category.addEventListener('change', refreshReplay);
  refreshReplay();
}

// Poll only our staff endpoint: opening the page never connects a hidden audio player.
const onAirPanel = document.querySelector('[data-on-air]');
if (onAirPanel) {
  const show = (selector, value) => { onAirPanel.querySelector(selector).textContent = value; };
  const unavailable = () => {
    show('[data-listener-count]', '—');
    show('[data-radio-kind]', 'Dati non aggiornati');
    show('[data-radio-title]', 'Titolo non disponibile');
    show('[data-radio-artist]', '');
    show('[data-radio-health]', 'Stato dello stream non disponibile');
    show('[data-engine-health]', 'Stato della regia non disponibile');
    show('[data-radio-updated]', 'Aggiornamento non disponibile. Nuovo tentativo tra 15 secondi.');
  };
  const refresh = async () => {
    try {
      const response = await fetch(onAirPanel.dataset.statusUrl, {
        credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(8000)
      });
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      show('[data-listener-count]', data.listeners ?? '—');
      show('[data-radio-kind]', data.kind);
      show('[data-radio-title]', data.title || 'Titolo non disponibile');
      show('[data-radio-artist]', data.artist);
      show('[data-radio-playlist]', data.playlist);
      show('[data-engine-health]', data.engine_online ? 'Regia collegata' : 'Stato della regia non disponibile');
      show('[data-radio-health]', data.stream_online === true ? 'Stream online' : data.stream_online === false ? 'Stream non disponibile' : 'Stato dello stream non disponibile');
      show('[data-radio-updated]', data.fresh ? 'Ultimo aggiornamento: '+new Date(data.checked_at).toLocaleTimeString('it-IT') : 'Dati non aggiornati');
      show('[data-radio-upcoming]', data.upcoming?.title || 'Nessuna trasmissione programmata.');
      show('[data-radio-upcoming-time]', data.upcoming ? new Date(data.upcoming.at).toLocaleString('it-IT',{timeZone:'Europe/Rome'})+' · ora di Roma' : '');
    } catch { unavailable(); }
    window.setTimeout(refresh, 15000);
  };
  refresh();
}

const backupsPanel = document.querySelector('[data-backups]');
if (backupsPanel?.dataset.active === 'yes') {
  const update = async () => {
    try {
      const response = await fetch(backupsPanel.dataset.statusUrl, {credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      if (data.version !== backupsPanel.dataset.version) { window.location.reload(); return; }
    } catch { /* A transient error must not clear the existing job status. */ }
    window.setTimeout(update, 5000);
  };
  window.setTimeout(update, 5000);
}

const broadcastForm = document.querySelector('[data-broadcast-form]');
if (broadcastForm) {
  const repeat = broadcastForm.querySelector('#id_repeat');
  const update = () => broadcastForm.querySelectorAll('[data-weekly-field]').forEach(field => {field.hidden = repeat?.value !== 'weekly';});
  repeat?.addEventListener('change', update);
  update();
}

// Native links keep dates keyboard-accessible; blank cell clicks are a shortcut.
document.querySelectorAll('[data-calendar-create]').forEach(cell => {
  cell.addEventListener('click', event => {
    if (event.target.closest('a,button,input,select,form')) return;
    if (window.getSelection()?.toString()) return;
    window.location.assign(cell.dataset.calendarCreate);
  });
});

const uploadMetadata = document.querySelector('[data-upload-metadata]');
if (uploadMetadata) {
  const upload = document.querySelector('[data-upload]');
  const files = upload.querySelector('[type=file]');
  const category = upload.querySelector('#id_category');
  const fallback = upload.querySelector('[data-upload-metadata-fallback]');
  const build = () => {
    uploadMetadata.replaceChildren();
    fallback.hidden = files.files.length > 0;
    fallback.querySelectorAll('input,textarea').forEach(field => {field.disabled = files.files.length > 0;});
    [...files.files].forEach((file, index) => {
      const row = document.createElement('fieldset');
      row.className = 'file-metadata-row';
      const legend = document.createElement('legend'); legend.textContent = file.name; row.append(legend);
      const label = document.createElement('label'); label.textContent = 'Titolo';
      const input = document.createElement('input'); input.type = 'text'; input.maxLength = 240;
      input.id = `file-title-${index}`;
      const suppliedTitle = files.files.length === 1 ? fallback.querySelector('input').value.trim() : '';
      input.value = suppliedTitle || file.name.replace(/\.[^.]+$/, '').replaceAll('_', ' ');
      if (suppliedTitle) input.name = `file_title_${index}`;
      label.htmlFor = input.id;
      // Untouched names remain automatic, so embedded title tags can still take precedence.
      input.addEventListener('input', () => {input.name = `file_title_${index}`;});
      row.append(label, input);
      const artistGroup = document.createElement('div'); artistGroup.dataset.artist = '';
      const artistLabel = document.createElement('label'); artistLabel.textContent = 'Artista (facoltativo)';
      const artist = document.createElement('input'); artist.type = 'text'; artist.maxLength = 240;
      artist.id = `file-artist-${index}`; artist.name = `file_artist_${index}`;
      if (files.files.length === 1) artist.value = fallback.querySelector('#id_artist').value;
      artistLabel.htmlFor = artist.id; artistGroup.append(artistLabel, artist); row.append(artistGroup);
      const descriptionLabel = document.createElement('label'); descriptionLabel.textContent = 'Descrizione (facoltativa)';
      const description = document.createElement('textarea'); description.rows = 3; description.maxLength = 5000;
      description.id = `file-description-${index}`; description.name = `file_description_${index}`;
      if (files.files.length === 1) description.value = fallback.querySelector('textarea').value;
      descriptionLabel.htmlFor = description.id; row.append(descriptionLabel, description);
      row.dataset.fileMetadata = '';
      uploadMetadata.append(row);
    });
    updateCategory();
  };
  const updateCategory = () => {
    const podcast = category.value === 'podcasts';
    const music = category.value === 'music';
    upload.querySelectorAll('[data-artist]').forEach(group => {
      group.hidden = !music;
      group.querySelector('input').disabled = !music || (group.closest('[data-upload-metadata-fallback]') !== null && files.files.length > 0);
    });
    uploadMetadata.querySelectorAll('textarea').forEach(field => {field.disabled = !podcast; field.hidden = !podcast; field.previousElementSibling.hidden = !podcast;});
    const fallbackDescription = fallback.querySelector('textarea');
    fallbackDescription.closest('[data-description]').hidden = !podcast;
    fallbackDescription.disabled = !podcast || files.files.length > 0;
  };
  files.addEventListener('change', build);
  category.addEventListener('change', updateCategory);
  updateCategory();
}
