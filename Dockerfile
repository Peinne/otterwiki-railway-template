FROM redimp/otterwiki:2.26.3@sha256:74740588190fa25dcb01ba4bde11e53d7a3db544d25a8afcb2e385fb5fbce3d7

# These paths must stay on the same persistent Railway volume. The inherited
# entrypoint creates the repository/settings and fixes www-data ownership.
ENV REPOSITORY=/app-data/repository \
    SQLALCHEMY_DATABASE_URI=sqlite:////app-data/db.sqlite \
    READ_ACCESS=APPROVED \
    WRITE_ACCESS=APPROVED \
    ATTACHMENT_ACCESS=APPROVED \
    DISABLE_REGISTRATION=true \
    AUTO_APPROVAL=false \
    ROBOTS_TXT=disallow \
    NGINX_MAX_UPLOAD=16M

COPY --chmod=0644 docker/bootstrap.py /opt/railway/bootstrap.py
COPY --chmod=0755 docker/start.sh /opt/railway/start.sh
# COPY can apply its file mode to a newly created destination directory.
# The application user must be able to traverse this directory after the copy.
RUN chmod 0755 /opt/railway \
    && test -x /usr/sbin/runuser \
    && /usr/sbin/runuser -u www-data -- test -r /opt/railway/bootstrap.py

# Keep the upstream ENTRYPOINT: no server is started until this CMD succeeds.
CMD ["/opt/railway/start.sh"]
