import React from 'react';
import { Clock, Users } from 'lucide-react';

const CourseCard = ({ courseName, teeTimes, image, onBook }) => {
    return (
        <div className="glass-panel" style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            {/* Image Header */}
            <div style={{
                height: '160px',
                background: `url(${image || '/golf-hero.png'}) center/cover no-repeat`,
                position: 'relative'
            }}>
                <div style={{
                    position: 'absolute',
                    bottom: 0,
                    left: 0,
                    right: 0,
                    background: 'linear-gradient(to top, rgba(0,0,0,0.8), transparent)',
                    padding: '1rem'
                }}>
                    <h3 style={{ color: 'white', fontWeight: '700', fontSize: '1.2rem', textShadow: '0 2px 4px rgba(0,0,0,0.5)' }}>
                        {courseName}
                    </h3>
                    <span style={{ fontSize: '0.8rem', color: 'rgba(255,255,255,0.8)' }}>
                        {teeTimes.length} Times Available
                    </span>
                </div>
            </div>

            {/* Times List */}
            <div style={{ padding: '1rem', overflowY: 'auto', maxHeight: '300px' }}>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: '0.5rem' }}>
                    {teeTimes.map((time, index) => (
                        <div key={index} style={{
                            background: 'rgba(255,255,255,0.05)',
                            borderRadius: '12px',
                            padding: '0.75rem',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '0.5rem',
                            border: '1px solid rgba(255,255,255,0.1)',
                            transition: 'all 0.2s'
                        }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                    <Clock size={14} color="#c5a059" />
                                    <span style={{ fontWeight: '600', fontSize: '1rem', color: 'white' }}>{time.time}</span>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                    <Users size={12} color="#94a3b8" />
                                    <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{time.slots}</span>
                                </div>
                            </div>

                            <button
                                className="glass-button"
                                style={{
                                    padding: '4px 8px',
                                    fontSize: '0.75rem',
                                    width: '100%',
                                    justifyContent: 'center',
                                    borderRadius: '6px'
                                }}
                                onClick={() => onBook && onBook(time)}
                            >
                                Book
                            </button>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
};

export default CourseCard;
