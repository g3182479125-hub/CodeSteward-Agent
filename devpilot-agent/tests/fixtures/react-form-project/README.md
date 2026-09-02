# React form G-02 fixture

This is a deliberately small static React fixture for P4-02. The defect is in
`src/App.jsx`: the submit handler does not call `event.preventDefault()`, so a
browser performs the default form navigation. The intended repair is limited to
that handler; no dependency installation or local execution is part of the
fixture evidence.
