# Suspend and resume broadcasting

Administrators can temporarily suspend broadcasting from **Manutenzione → Stato della radio**. Confirm the action before using the button.

Suspension immediately stops the public stream and interrupts any podcast in progress. It leaves audio files, accounts and station settings unchanged. Due appointments are marked as skipped and are never queued for catch-up. Interrupted podcasts do not count as completed for automatic Replay publication.

**Riprendi la trasmissione** starts the stream again with the normal source priority: a connected live source takes priority, otherwise music resumes. An interrupted podcast is not resumed automatically. Listeners may need to press play again after the stream disconnects.

Suspension belongs to the current Liquidsoap session. Restarting the dashboard preserves it; restarting the audio engine or server clears it automatically. Reset and restore maintenance stops remain separate and are not bypassed by these controls.

If the engine does not confirm a command, the page reports the error and calendar dispatch remains blocked until the action is retried or the audio engine restarts. Use **Aggiorna** to reread the status.
