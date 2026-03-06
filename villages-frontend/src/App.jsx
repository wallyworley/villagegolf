import React, { useState, useEffect } from 'react';
import { Search, Calendar, Loader2, User, Users, Lock, LogIn, Award, CheckCircle, ArrowLeft, RotateCcw, Cloud, CloudRain, Sun, Thermometer } from 'lucide-react';
import CourseCard from './components/CourseCard';

function App() {
  // Screens: 'login', 'buddies', 'search', 'success'
  const [screen, setScreen] = useState('login');

  // Credentials
  const [creds, setCreds] = useState(() => {
    const saved = localStorage.getItem('the_bubble_creds');
    return saved ? JSON.parse(saved) : { username: '', password: '', pin: '' };
  });

  // State
  const [buddies, setBuddies] = useState([]);
  const [courseTypes, setCourseTypes] = useState([]); // Championship/Executive options
  const [selectedBuddies, setSelectedBuddies] = useState([]); // IDs
  const [selectedCourseType, setSelectedCourseType] = useState(''); // Course type ID
  const [date, setDate] = useState('');
  const [loading, setLoading] = useState(false);
  const [loadingText, setLoadingText] = useState('');
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);
  const [lastBooking, setLastBooking] = useState(null);
  const [weather, setWeather] = useState(null);
  const [viewMode, setViewMode] = useState('list'); // 'list' or 'sheet'

  // Persistence side-effect
  useEffect(() => {
    if (creds.username) {
      localStorage.setItem('the_bubble_creds', JSON.stringify(creds));
    }
  }, [creds]);

  // Fetch Weather Effect
  useEffect(() => {
    if (date) {
      fetch(`http://localhost:8080/weather?date=${date}`)
        .then(res => res.json())
        .then(data => {
          if (data.success) setWeather(data.weather);
        })
        .catch(err => console.error("Weather fetch failed:", err));
    }
  }, [date]);

  // Date Restrictions (Next 3 days only)
  useEffect(() => {
    const today = new Date();
    const tomorrow = new Date(today);
    tomorrow.setDate(today.getDate() + 1);

    const maxDate = new Date(tomorrow);
    maxDate.setDate(tomorrow.getDate() + 2);

    const minStr = tomorrow.toISOString().split('T')[0];
    const maxStr = maxDate.toISOString().split('T')[0];

    // Set default date to tomorrow if not set
    if (!date) setDate(minStr);

    // Expose these for the input
    window.minDateAttr = minStr;
    window.maxDateAttr = maxStr;
  }, []);

  // --- Step 1: Fetch Buddies (Login) ---
  const handleLogin = async (e) => {
    e.preventDefault();
    setLoading(true);
    setLoadingText('Logging in & Fetching Buddies...');
    setError(null);

    try {
      const response = await fetch('http://localhost:8080/fetch-buddies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(creds),
      });

      const data = await response.json().catch(() => null);
      if (!response.ok || !data || !data.success) {
        throw new Error(data?.message || data?.error || `Login Error: ${response.status} ${response.statusText}`);
      }

      setBuddies(data.buddies);
      setCourseTypes(data.courseTypes || []);
      if (data.courseTypes && data.courseTypes.length > 0) {
        setSelectedCourseType(data.courseTypes[0].id);
      }
      setScreen('buddies');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // --- Step 2: Select Buddies ---
  const toggleBuddy = (id) => {
    if (selectedBuddies.includes(id)) {
      setSelectedBuddies(selectedBuddies.filter(b => b !== id));
    } else {
      setSelectedBuddies([...selectedBuddies, id]);
    }
  };

  const proceedToSearch = () => {
    setScreen('search');
  };

  const resetToStart = () => {
    setResults(null);
    setLastBooking(null);
    setScreen('buddies');
  };

  // --- Step 3: Search ---
  const fetchTeeTimes = async () => {
    setLoading(true);
    setLoadingText('Finding Tee Times...');
    setError(null);
    setResults(null);

    const formattedDate = date.replace(/-/g, '');

    try {
      const response = await fetch('http://localhost:8080/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...creds,
          date: formattedDate,
          golfers: selectedBuddies,
          courseType: selectedCourseType
        }),
      });

      const data = await response.json().catch(() => null);
      if (!response.ok || !data || !data.success) {
        throw new Error(data?.message || data?.error || `Server Error: ${response.status} ${response.statusText}`);
      }

      const grouped = data.data.reduce((acc, curr) => {
        const name = curr.course.trim();
        if (!acc[name]) acc[name] = [];
        acc[name].push(curr);
        return acc;
      }, {});

      setResults(grouped);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // --- Step 4: Booking ---
  // --- HELPERS ---
  const formatDate = (dateStr) => {
    if (!dateStr) return '';
    const [year, month, day] = dateStr.split('-');
    return `${month}/${day}/${year}`;
  };

  const getCourseImage = (name) => {
    const n = name.toLowerCase();
    if (n.includes('garden') || n.includes('havana') || n.includes('bonifay')) return '/images/tropical.png';
    if (n.includes('hacienda') || n.includes('tierra') || n.includes('blossom')) return '/images/spanish.png';
    if (n.includes('legend') || n.includes('legacy') || n.includes('champion')) return '/images/lush.png';
    if (n.includes('prairie') || n.includes('glade') || n.includes('mallory') || n.includes('belle')) return '/images/sunset.png';
    if (n.includes('wood') || n.includes('pine') || n.includes('saddle') || n.includes('oak')) return '/images/wooded.png';
    if (n.includes('view') || n.includes('water') || n.includes('lake')) return '/images/water.png';
    if (n.includes('sand') || n.includes('trap') || n.includes('bunker')) return '/images/bunker.png';

    // Deterministic fallback for variety
    const images = ['/images/lush.png', '/images/mist.png', '/images/sunset.png', '/images/wooded.png'];
    const hash = name.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0);
    return images[hash % images.length];
  };

  const handleBook = async (time, courseName) => {
    setLoading(true);
    setLoadingText(`Booking ${time.time} at ${courseName}...`);
    setError(null);
    try {
      const response = await fetch('http://localhost:8080/book', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...creds,
          date,
          golfers: selectedBuddies,
          courseType: selectedCourseType,
          bookingId: time.bookingId,
          time: time.time,
          course: courseName
        }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data || !data.success) {
        if (!response.ok && response.status === 503) {
          throw new Error("The booking service is currently unavailable or timed out. Please check your reservation status on the official website or try again in a few minutes.");
        }
        throw new Error(data?.message || data?.error || `Booking Error: ${response.status} ${response.statusText}`);
      }

      setLastBooking({
        resNumber: data.reservationNumber || 'N/A',
        time: time.time,
        course: courseName,
        date: date
      });
      setScreen('success');
    } catch (err) {
      setError(`Booking Failed: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  // --- RENDER ---
  return (
    <div className="container" style={{ minHeight: '100vh', paddingBottom: '4rem' }}>

      {/* Header */}
      <header className="glass-panel" style={{ padding: '2rem', marginBottom: '2rem', textAlign: 'center' }}>
        <h1 style={{ background: 'linear-gradient(to right, #c5a059, #f8fafc)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
          The Bubble
        </h1>
        <p style={{ color: 'var(--text-secondary)' }}>Premium Golf Concierge</p>
      </header>

      {/* Main Content Area */}
      <main style={{ maxWidth: '800px', margin: '0 auto' }}>

        {/* Error Banner */}
        {error && (
          <div className="glass-panel" style={{ padding: '1rem', textAlign: 'center', borderColor: '#ef4444', marginBottom: '1rem', background: 'rgba(239, 68, 68, 0.1)' }}>
            <h3 style={{ color: '#ef4444', fontSize: '1rem' }}>{error}</h3>
          </div>
        )}

        {/* LOADING */}
        {loading && (
          <div className="glass-panel" style={{ padding: '4rem', textAlign: 'center' }}>
            <Loader2 className="animate-spin" size={48} color="#c5a059" style={{ margin: '0 auto 1rem' }} />
            <h2 style={{ color: 'white' }}>{loadingText}</h2>
            <p style={{ color: '#94a3b8' }}>Please wait while we communicate with the golf system...</p>
          </div>
        )}

        {/* Screen 1: LOGIN */}
        {!loading && screen === 'login' && (
          <div className="glass-panel" style={{ padding: '2rem' }}>
            <h2 className="text-center">Member Login</h2>
            <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: '1rem', maxWidth: '400px', margin: '0 auto' }}>

              <div style={{ position: 'relative' }}>
                <User size={18} style={{ position: 'absolute', left: '12px', top: '14px', color: '#94a3b8' }} />
                <input
                  type="text"
                  placeholder="Username"
                  value={creds.username}
                  onChange={e => setCreds({ ...creds, username: e.target.value })}
                  style={{ width: '100%', paddingLeft: '40px' }}
                  required
                />
              </div>

              <div style={{ position: 'relative' }}>
                <Lock size={18} style={{ position: 'absolute', left: '12px', top: '14px', color: '#94a3b8' }} />
                <input
                  type="password"
                  placeholder="Password"
                  value={creds.password}
                  onChange={e => setCreds({ ...creds, password: e.target.value })}
                  style={{ width: '100%', paddingLeft: '40px' }}
                  required
                />
              </div>

              <div style={{ position: 'relative' }}>
                <p style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: '4px' }}>Security PIN</p>
                <input
                  type="text"
                  placeholder="1234"
                  value={creds.pin}
                  onChange={e => setCreds({ ...creds, pin: e.target.value })}
                  style={{ width: '100%', textAlign: 'center', letterSpacing: '4px', fontSize: '1.2rem' }}
                  maxLength={4}
                  required
                />
              </div>

              <button type="submit" className="glass-button" style={{ marginTop: '1rem', justifyContent: 'center', display: 'flex' }}>
                <LogIn size={20} style={{ marginRight: '8px' }} />
                Login & Fetch Buddies
              </button>
            </form>
          </div>
        )}

        {/* Screen 2: BUDDY SELECTION */}
        {!loading && screen === 'buddies' && (
          <div className="glass-panel" style={{ padding: '2rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
              <h2 style={{ margin: 0 }}>Select Your Group</h2>
              <span className="glass-button" style={{ fontSize: '0.8rem', padding: '6px 12px' }}>
                {selectedBuddies.length} Selected
              </span>
            </div>

            <p style={{ color: '#94a3b8', marginBottom: '1rem' }}>
              Select who is playing. We will find tee times for {selectedBuddies.length} or more players.
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '1rem', marginBottom: '2rem' }}>

              {buddies.map(buddy => {
                const isSelected = selectedBuddies.includes(buddy.id);
                return (
                  <div
                    key={buddy.id}
                    onClick={() => toggleBuddy(buddy.id)}
                    style={{
                      padding: '1rem', borderRadius: '8px',
                      background: isSelected ? 'rgba(45, 212, 191, 0.2)' : 'rgba(255,255,255,0.05)',
                      border: isSelected ? '1px solid #c5a059' : '1px solid transparent',
                      cursor: 'pointer',
                      display: 'flex', alignItems: 'center', gap: '0.5rem',
                      transition: 'all 0.2s'
                    }}
                  >
                    <Users size={20} color={isSelected ? "#c5a059" : "#64748b"} />
                    <span>{buddy.name}</span>
                  </div>
                )
              })}
            </div>

            {/* Course Type Selection */}
            {courseTypes.length > 0 && (
              <div style={{ marginBottom: '2rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                  <Award size={20} color="#c5a059" />
                  <h3 style={{ margin: 0, fontSize: '1rem' }}>Course Type</h3>
                </div>
                <select
                  value={selectedCourseType}
                  onChange={(e) => setSelectedCourseType(e.target.value)}
                  style={{ width: '100%', padding: '12px', borderRadius: '8px' }}
                >
                  {courseTypes.map(ct => (
                    <option key={ct.id} value={ct.id} style={{ color: 'black' }}>
                      {ct.name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <button className="glass-button" style={{ width: '100%', justifyContent: 'center', display: 'flex' }} onClick={proceedToSearch}>
              Continue to Date Selection
            </button>
          </div>
        )}

        {/* Screen 3: SEARCH */}
        {!loading && screen === 'search' && (
          <>
            <div style={{ marginBottom: '1rem' }}>
              <button onClick={() => setScreen('buddies')} className="glass-button" style={{ padding: '8px 16px', fontSize: '0.9rem' }}>
                <ArrowLeft size={16} /> Back to Group Selection
              </button>
            </div>

            {/* Search Bar */}
            <div className="glass-panel" style={{ padding: '1.5rem', marginBottom: '2rem', display: 'flex', gap: '1rem', alignItems: 'center', justifyContent: 'center', flexWrap: 'wrap' }}>

              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#94a3b8' }}>
                <Users size={18} />
                <span>Looking for <strong>{selectedBuddies.length}</strong> Players</span>
              </div>

              <div style={{ width: '1px', height: '20px', background: 'rgba(255,255,255,0.2)' }}></div>

              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', background: 'rgba(0,0,0,0.2)', padding: '0.5rem 1rem', borderRadius: '8px' }}>
                <Calendar color="#c5a059" size={20} />
                <input
                  type="date"
                  value={date}
                  min={window.minDateAttr}
                  max={window.maxDateAttr}
                  onChange={(e) => setDate(e.target.value)}
                  style={{ border: 'none', background: 'transparent', color: 'white', padding: '0', fontSize: '1rem' }}
                />
              </div>

              {/* Weather Summary */}
              {weather && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.8rem', background: 'rgba(255,255,255,0.05)', padding: '0.5rem 1rem', borderRadius: '8px', border: '1px solid rgba(45, 212, 191, 0.2)' }}>
                  {weather.code < 3 ? <Sun size={20} color="#c5a059" /> : weather.code < 50 ? <Cloud size={20} color="#94a3b8" /> : <CloudRain size={20} color="#38bdf8" />}
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <span style={{ fontSize: '0.8rem', color: '#94a3b8' }}>Forecast</span>
                    <span style={{ fontSize: '0.9rem', fontWeight: 'bold' }}>{weather.tempMax}°F / {weather.tempMin}°F</span>
                  </div>
                </div>
              )}

              <div style={{ width: '1px', height: '32px', background: 'rgba(255,255,255,0.2)' }}></div>

              <div style={{ display: 'flex', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', padding: '4px' }}>
                <button
                  onClick={() => setViewMode('list')}
                  style={{
                    padding: '6px 12px', border: 'none', borderRadius: '6px', cursor: 'pointer',
                    background: viewMode === 'list' ? '#c5a059' : 'transparent',
                    color: viewMode === 'list' ? 'black' : 'white',
                    transition: 'all 0.2s'
                  }}
                >List</button>
                <button
                  onClick={() => setViewMode('sheet')}
                  style={{
                    padding: '6px 12px', border: 'none', borderRadius: '6px', cursor: 'pointer',
                    background: viewMode === 'sheet' ? '#c5a059' : 'transparent',
                    color: viewMode === 'sheet' ? 'black' : 'white',
                    transition: 'all 0.2s'
                  }}
                >Visual</button>
              </div>

              <button
                onClick={fetchTeeTimes}
                disabled={loading}
                className="glass-button"
                style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}
              >
                <Search size={20} />
                Find Tee Times
              </button>
            </div>

            {/* Results */}
            {results ? (
              viewMode === 'list' ? (
                <div className="grid-cols-auto">
                  {Object.keys(results).sort().map((courseName) => (
                    <CourseCard
                      key={courseName}
                      courseName={courseName}
                      image={getCourseImage(courseName)}
                      teeTimes={results[courseName]}
                      onBook={(time) => handleBook(time, courseName)}
                    />
                  ))}
                  {Object.keys(results).length === 0 && (
                    <div style={{ gridColumn: '1/-1', textAlign: 'center', padding: '2rem' }}>
                      <p>No tee times found for this date matching your group size.</p>
                    </div>
                  )}
                </div>
              ) : (
                /* Visual Tee Sheet View */
                <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
                  {Object.keys(results).sort().map((courseName) => {
                    const timesByHour = results[courseName].reduce((acc, t) => {
                      const hour = t.time.split(':')[0];
                      if (!acc[hour]) acc[hour] = [];
                      acc[hour].push(t);
                      return acc;
                    }, {});

                    return (
                      <div key={courseName} className="glass-panel" style={{ padding: '1.5rem' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem', borderBottom: '1px solid rgba(255,255,255,0.1)', paddingBottom: '0.8rem' }}>
                          <h3 style={{ margin: 0, color: '#c5a059', display: 'flex', alignItems: 'center', gap: '0.8rem' }}>
                            <div style={{ width: '40px', height: '40px', borderRadius: '8px', overflow: 'hidden', border: '1px solid rgba(45,212,191,0.3)' }}>
                              <img src={getCourseImage(courseName)} style={{ width: '100%', height: '100%', objectFit: 'cover' }} alt="" />
                            </div>
                            {courseName}
                          </h3>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '1.5rem' }}>
                          {Object.keys(timesByHour).sort().map(hour => (
                            <div key={hour} style={{ background: 'rgba(255,255,255,0.02)', padding: '1rem', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
                              <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginBottom: '0.8rem', fontWeight: 'bold', textTransform: 'uppercase', letterSpacing: '1px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                                <Calendar size={12} /> {hour}:00 Wave
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                                {timesByHour[hour].map(t => (
                                  <button
                                    key={t.bookingId}
                                    onClick={() => handleBook(t, courseName)}
                                    className="glass-button"
                                    style={{
                                      padding: '10px', fontSize: '1rem', justifyContent: 'center', width: '100%',
                                      background: 'rgba(45, 212, 191, 0.1)',
                                      borderColor: 'rgba(45, 212, 191, 0.2)'
                                    }}
                                  >
                                    <span style={{ fontWeight: 'bold' }}>{t.time}</span>
                                    <span style={{ fontSize: '0.7rem', opacity: 0.6, marginLeft: '4px' }}>{t.slots} Avail</span>
                                  </button>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )
            ) : (
              <div style={{ textAlign: 'center', opacity: 0.5, marginTop: '2rem' }}>
                <p>Ready to search for {selectedBuddies.length} golfers.</p>
              </div>
            )}
          </>
        )}

        {/* Screen 4: SUCCESS */}
        {!loading && screen === 'success' && lastBooking && (
          <div className="glass-panel" style={{ padding: '3rem', textAlign: 'center', border: '2px solid #c5a059' }}>
            <CheckCircle size={64} color="#c5a059" style={{ margin: '0 auto 1.5rem' }} />
            <h2 style={{ fontSize: '2rem', marginBottom: '0.5rem' }}>Booking Confirmed!</h2>
            <p style={{ color: '#94a3b8', marginBottom: '2rem' }}>Your reservation has been successfully registered.</p>

            <div style={{ background: 'rgba(255,255,255,0.05)', borderRadius: '12px', padding: '1.5rem', marginBottom: '2.5rem', textAlign: 'left' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem', borderBottom: '1px solid rgba(255,255,255,0.1)', paddingBottom: '0.5rem' }}>
                <span style={{ color: '#94a3b8' }}>Reservation Number</span>
                <strong style={{ color: '#c5a059', fontSize: '1.2rem' }}>
                  {lastBooking.resNumber && lastBooking.resNumber !== 'N/A' && lastBooking.resNumber !== 'Pending'
                    ? `#${lastBooking.resNumber}`
                    : <span style={{ fontSize: '1rem', color: '#fbbf24' }}>Pending / Check Email</span>}
                </strong>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.8rem' }}>
                <span style={{ color: '#94a3b8' }}>Course</span>
                <span>{lastBooking.course}</span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.8rem' }}>
                <span style={{ color: '#94a3b8' }}>Time</span>
                <span>{lastBooking.time}</span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: '#94a3b8' }}>Date</span>
                <span>{formatDate(lastBooking.date)}</span>
              </div>
            </div>

            <div style={{ display: 'flex', gap: '1rem', justifyContent: 'center' }}>
              <button className="glass-button" onClick={() => setScreen('search')}>
                <Search size={18} /> Back to Search
              </button>
              <button className="glass-button" style={{ background: 'var(--accent-primary)', color: 'black' }} onClick={resetToStart}>
                <RotateCcw size={18} /> Make Another Reservation
              </button>
            </div>
          </div>
        )}

      </main>
    </div>
  );
}

export default App;
