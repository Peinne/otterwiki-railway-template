FROM redimp/otterwiki:2.27.0@sha256:9cf1d76c6bff097ff924404aa5a149b235b15aaa05553870e96504cd1d92752b

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
