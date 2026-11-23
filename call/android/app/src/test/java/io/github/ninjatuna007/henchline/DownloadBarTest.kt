package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DownloadBarTest {
    @Test fun barFollowsTheJobsPhase() {
        assertEquals(DownloadPhase.CHECKING, barPhase(DownloadPhase.CHECKING, running = true, bytes = 0))
        assertEquals(DownloadPhase.DOWNLOADING, barPhase(DownloadPhase.DOWNLOADING, true, 5_000_000))
        assertEquals(DownloadPhase.CHECKING, barPhase(DownloadPhase.CHECKING, true, 64_000_000))  // the last file's hash
        assertEquals(DownloadPhase.CHECKING, barPhase(null, true, 0))  // just started, nothing told yet
        assertEquals(DownloadPhase.WAITING_WIFI, barPhase(DownloadPhase.WAITING_WIFI, false, 28_800_000))
    }

    @Test fun noBarWhenNothingIsUnderWay() {
        assertNull(barPhase(null, false, 0))
        assertNull(barPhase(null, false, 28_800_000))  // cancelled or failed: the words say it
        assertNull(barPhase(DownloadPhase.WAITING_WIFI, false, 0))  // nothing here yet: the words say it
        assertNull(barPhase(DownloadPhase.DONE, false, 64_000_000))  // the sheet shows it ready
    }

    @Test fun percentIsWholeAndHeld() {
        assertEquals(0, downloadPercent(0, 0))
        assertEquals(0, downloadPercent(5, 0))
        assertEquals(44, downloadPercent(28_899_999, 64_222_222))  // 44.99: cut down
        assertEquals(45, downloadPercent(28_900_000, 64_222_222))
        assertEquals(99, downloadPercent(63_999_999, 64_000_000))  // never 100 before it is
        assertEquals(100, downloadPercent(70_000_000, 64_000_000))
        assertEquals(0, downloadPercent(-1, 64_000_000))
    }

    @Test fun megabytesAreCutDownNotRoundedUp() {
        assertEquals("28.8", megabytes(28_899_999, tenths = true))
        assertEquals("0.0", megabytes(99_999, true))
        assertEquals("64.0", megabytes(64_000_000, true))
        assertEquals("311", megabytes(311_999_999, false))
        assertEquals("0", megabytes(-5, false))
    }

    @Test fun amountSaysPercentAndExactMegabytes() {
        assertEquals("", downloadAmount(0, 0))  // the laptop's list not read yet
        assertEquals("0% · 0.0 of 64.2 MB", downloadAmount(0, 64_222_222))
        assertEquals("45% · 28.9 of 64.2 MB", downloadAmount(28_900_000, 64_222_222))
        assertEquals("100% · 64.2 of 64.2 MB", downloadAmount(64_222_222, 64_222_222))
        assertEquals("47% · 312 of 664 MB", downloadAmount(312_400_000, 664_100_000))  // whole MB from 100 up
        assertEquals("100% · 664 of 664 MB", downloadAmount(700_000_000, 664_100_000))
    }

    @Test fun spokenNamesThePhaseAndPercent() {
        assertEquals("Downloading, 45 percent", downloadSpoken(DownloadPhase.DOWNLOADING, 45))
        assertEquals("Waiting for Wi-Fi, 12 percent", downloadSpoken(DownloadPhase.WAITING_WIFI, 12))
        assertEquals("Checking files", downloadSpoken(DownloadPhase.CHECKING, 0))
        assertEquals("Download done", downloadSpoken(DownloadPhase.DONE, 100))
    }

    @Test fun aScreenReaderHearsTheStartThePauseAndTheEndOnly() {
        val d = DownloadPhase.DOWNLOADING; val c = DownloadPhase.CHECKING
        val w = DownloadPhase.WAITING_WIFI; val done = DownloadPhase.DONE
        assertNull(downloadAnnouncement(null, c, 0, saidDownloading = false))  // a bar just drawn: silent
        assertNull(downloadAnnouncement(null, d, 40, false))  // a sheet opened mid-download: silent
        assertEquals("Downloading, 3 percent", downloadAnnouncement(c, d, 3, saidDownloading = false))
        assertNull(downloadAnnouncement(d, c, 50, true))  // checking is never said
        assertNull(downloadAnnouncement(c, d, 51, saidDownloading = true))  // nor Downloading a second time
        assertNull(downloadAnnouncement(d, d, 60, true))  // ticks are not said
        assertEquals("Waiting for Wi-Fi, 45 percent", downloadAnnouncement(d, w, 45, true))
        assertEquals("Waiting for Wi-Fi, 45 percent", downloadAnnouncement(null, w, 45, false, justPaused = true))
        assertNull(downloadAnnouncement(null, w, 45, false))  // a sheet opened while it waits: silent
        assertEquals("Download done", downloadAnnouncement(c, done, 100, true))
    }

    @Test fun fillFollowsTheAmount() {
        assertEquals(0.45f, fillTarget(0f, 45, 100), 1e-6f)
        assertEquals(0.3f, fillTarget(0.45f, 30, 100), 1e-6f)  // a new run's real amount, glided back to
        assertEquals(1f, fillTarget(0.5f, 120, 100), 1e-6f)
        assertEquals(0f, fillTarget(0f, -5, 100), 1e-6f)
        assertEquals(0.3f, fillTarget(0.3f, 50, 0), 1e-6f)  // size not known yet: stays where it was
    }

    @Test fun fillIsAtLeastADotOnceStarted() {
        assertEquals(0f, fillWidth(0f, 300f, 8f), 1e-6f)
        assertEquals(8f, fillWidth(0.01f, 300f, 8f), 1e-6f)
        assertEquals(150f, fillWidth(0.5f, 300f, 8f), 1e-6f)
        assertEquals(300f, fillWidth(1.2f, 300f, 8f), 1e-6f)
        assertEquals(0f, fillWidth(0.5f, 0f, 8f), 1e-6f)
    }

    @Test fun shimmerCrossesTheWholeTrack() {
        assertEquals(-100f, shimmerAt(0f, 300f, 100f), 1e-6f)  // just off the left
        assertEquals(300f, shimmerAt(1f, 300f, 100f), 1e-6f)  // just off the right
        assertEquals(100f, shimmerAt(0.5f, 300f, 100f), 1e-6f)
    }
}
